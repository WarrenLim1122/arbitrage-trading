# 05 — Data Contracts (exact schemas — do not deviate)

All schemas below are extracted verbatim from the live reference code. Match them exactly so the same
TradingView alerts and the same transport work unchanged.

---

## 1. Webhook payload — TradingView → Receiver `/signal` (14 fields)

The Receiver's Pydantic model must accept the **14-field superset** (reference `layer2/logic_core.py:1006`).
TradingView posts this JSON on a breakout.

```python
class SignalPayload(BaseModel):
    signal:         str    # "LONG" | "SHORT"   — enum-validated (.upper())
    ticker:         str    # must be in the canonical registry (config/symbols.json) — enum-validated
    timestamp_ms:   int
    timeframe:      str
    entry:          float  # > 0
    sl:             float  # > 0
    tp:             float  # > 0
    sl_pips:        float  # rides into the worker ticket (journal); not used for sizing
    rr_ratio:       float  # inert
    order_type:     str    # inert ("LIMIT")
    daily_trend:    str    # inert
    m15_swing_high: float  # inert (sent as literal 0 to avoid na→"NaN")
    m15_swing_low:  float  # inert
    pip_type:       str    # inert
```

**Validation rules (exact):**
- `signal` upper-cased, must be `LONG` or `SHORT` else 422.
- `ticker` upper-cased, must be in `ALLOWED_PAIRS` (from `config/symbols.json`) else 422.
- `entry`, `sl`, `tp` must each be `> 0` else 422.
- All other fields: correct JSON type only (functionally inert but **required** — a missing field 422s).
- **The na→"NaN" trap:** Pine `str.tostring(na)` emits `"NaN"` → invalid JSON → 422. The frozen
  indicator already defaults numerics to 0. Don't change the indicator.

**Sizing consumes ONLY `entry`, `sl`, `tp`** (+ live broker tick data). Everything else is journal/inert.

---

## 2. ZMQ execution ticket — Receiver PUSH :5555 → Worker PULL

Exact shape (reference `layer2/logic_core.py:1548`). Single leg → send **one** ticket per signal
(the reference sent two; you send only the personal one, direction = signal).

```python
ticket = {
    "signal_id":    f"{base_id}_pers",   # unique id; base_id from timestamp+ticker
    "ticker":       payload.ticker,       # canonical name; Worker maps to broker symbol
    "timestamp_ms": payload.timestamp_ms,
    "entry":        payload.entry,
    "sl":           geometry["sl"],       # = signal_sl
    "tp":           geometry["tp"],       # = signal_tp
    "sl_pips":      payload.sl_pips,
    "signal":       geometry["direction"],# = the signal direction (LONG/SHORT), NOT inverted
    "lots":         geometry["lots"],
    "order_type":   "market",
}
```

Serialize as JSON, PUSH on :5555. Fire-and-forget; confirm fill via the `order_status` query.

---

## 3. ZMQ REP query protocol — Receiver REQ :5556 → Worker (timeout 3s)

Reference `_worker_core.py:1480`. Implement these query types in `worker/queries.py`. Request is a JSON
object with a `query` field (default `equity`) + params; reply is JSON.

| `query` | Params | Reply contains |
|---|---|---|
| `equity` | `ticker`, `want_fee` (bool) | `balance`, `equity`, `profit`, contract info (`contract_size`, `trade_tick_size`, `trade_tick_value`, `digits`), `account_currency`, `usd_to_acct_rate`, `trade_allowed`; + fee fields iff `want_fee` |
| `positions` | — | list of open positions |
| `order_status` | `signal_id` | stored execution result (`filled`/`rejected`/`pending` + fill price) |
| `order_check` | `ticker`, `signal`, `lots`, `sl`, `tp` | pre-flight: `verdict` ∈ {ok, reject, transient}, margin info |
| `deal_pnl` | `symbol`, `ticket` | realized `gross`/`commission`/`swap`/`net` for a closed position; `found` bool |
| `account_mode` | — | `demo`/`real`/`contest`/`unknown` |
| `checksymbols` | — | per-broker SUPPORTED/FOUND/MISSING list |
| `reset_fee_anchor` | — | re-anchors per-cycle fee (fee→0); fire after baseline change |

**Critical reply details (carry these over — they cost real debugging time in the reference):**
- `deal_pnl` matches **strictly by `position_id` (ticket) + `DEAL_ENTRY_OUT`**, never symbol+latest.
  Use `to_dt = UTC-now + 1 day` for the deal-history window — MT5 `deal.time` is **server-tz, not UTC**.
  If the exit deal hasn't surfaced, return `found=False` (caller shows `(est.)`, not a wrong number).
- `equity` fee scan (`want_fee=True` only): `trading_fee = (balance − Σ all deal.profit) − fee_anchor`.
  Same `now + 1 day` server-tz window. Gated to `/equity` only, never the 30s poll.

---

## 4. `config/personal_config.json` (the single source of truth)

```jsonc
{
  "personal_baseline": 0.0,            // SGD; immutable risk anchor; Telegram-set; NEVER live equity
  "active_mode": "conservative",
  "modes": {
    "conservative": { "risk_pct": 0.01 },   // CONFIRM with Warren
    "aggressive":   { "risk_pct": 0.02 }     // CONFIRM with Warren
  },
  "max_lots": 0.0,                     // 0 = uncapped (mirror of reference max_prop_lots guard)
  "max_open_positions": 2,             // counts PERSONAL open positions
  "daily_dd_pct": 4.0,                 // CONFIRM — daily DD halt (resets each session)
  "overall_dd_pct": 8.0,               // CONFIRM — permanent overall DD halt
  "day_roll": "11:00",                 // SGT HH:MM session reset
  "trading_window": { "current": null, "next": null },  // SGT HH:MM strings or null
  "active": true,                      // master on/off (/start /stop)
  "permanently_halted": false,
  "daily_halted": false,
  "daily_halted_date": null,
  "soft_kill_override_day": null,      // set by /resume; suppresses daily halt for the rest of the day
  "day_start_equity": 0.0,             // snapshotted at the day roll
  "day_start_date_utc": null,
  "deposit": 0.0,                      // actual capital; reporting/% only; ZERO effect on sizing
  "prop_halt_listener": {              // NEW (see 10-prop-halt-listener.md); independent of personal's own halts
    "enabled": true,
    "group_chat_id": null,             // shared Telegram group both bots sit in
    "prop_bot_username": null,         // only act on messages from this sender
    "keyword_map": {                   // prop alert keyword → kill id (override if prop wording changes)
      "K1": ["KILL 1", "K1", "Daily Loss"], "K2": ["KILL 2", "K2", "Overall Drawdown"],
      "K3": ["KILL 3", "K3", "Daily Profit Cap"], "K4": ["KILL 4", "K4", "Profit Target"],
      "K5": ["KILL 5", "K5", "Consistency"], "FORCE": ["FORCE_CLOSE", "HALT"]
    },
    "action": {                        // CONFIRM at CP-1
      "pair_named": "close_pair",      // close only the named pair's personal position
      "account_wide_permanent": "close_all_and_halt",  // K2/K4/K5
      "account_wide_daily": "close_all"                // K1/K3
    }
  }
}
```

Rules: `personal_baseline` and `risk_pct` drive sizing; **live equity is used only for halts and
reporting, never sizing**. `account_currency` is NOT stored — it's read live from MT5 (`equity` reply)
so a broker currency change needs no code/config edit.

---

## 5. Worker `.env` (gitignored, `secrets/.env`)

```ini
MT5_LOGIN=<personal account login>          # hard guard: account_info().login must equal this
MT5_TERMINAL_PATH=                           # set only if multiple MT5 installs on the VPS
ZMQ_PULL_BIND=tcp://0.0.0.0:5555
ZMQ_REP_BIND=tcp://0.0.0.0:5556
FIREBASE_JOURNAL_ENABLED=true                # MUST be 'true' or the close watcher never starts (silent)
FIREBASE_JOURNAL_DRY_RUN=false               # 'true' = log payload, skip Firestore write
GOOGLE_APPLICATION_CREDENTIALS=secrets/firebase-service-account.json
```

Receiver `.env`:
```ini
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat id>
ZMQ_PUSH_CONNECT=tcp://<worker_ip>:5555
ZMQ_REQ_CONNECT=tcp://<worker_ip>:5556
NEWS_WINDOW=<minutes>                         # high-impact news suppression half-width
FINNHUB_TOKEN=<token>                         # for ff_calendar news feed
```

> **Journaling silent-death guard:** on worker startup, log a WARNING if `FIREBASE_JOURNAL_ENABLED`≠`true`
> or `FIREBASE_JOURNAL_DRY_RUN`=`true`. After any `.env` rebuild these must be re-set or journaling dies
> silently. (Reference: `_worker_core.py:1739`.)
