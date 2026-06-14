# 05 — Data Contracts (exact schemas)

Extracted verbatim from the live reference code. Single-account.

## 1. Webhook payload — TradingView → Receiver `/signal` (14 fields)
Same 14-field superset as the reference (`logic_core.py:1006`). The system's own Pine emits it.
```python
class SignalPayload(BaseModel):
    signal:         str    # "LONG" | "SHORT" — enum-validated (.upper())
    ticker:         str    # must be in config/symbols.json registry — enum-validated
    timestamp_ms:   int
    timeframe:      str
    entry:          float  # > 0
    sl:             float  # > 0   (the tight stop)
    tp:             float  # > 0   (the far target)
    sl_pips:        float  # rides into the ticket (journal)
    rr_ratio:       float  # inert
    order_type:     str    # inert
    daily_trend:    str    # inert
    m15_swing_high: float  # inert (literal 0 to avoid na→"NaN")
    m15_swing_low:  float  # inert
    pip_type:       str    # inert
```
Validation: `signal` ∈ {LONG,SHORT}; `ticker` ∈ registry; `entry`/`sl`/`tp` > 0; other fields type-only
but required. **Sizing consumes only `entry`,`sl`,`tp` + live tick data.** Pine `str.tostring(na)`→`"NaN"`
is a 422 trap — keep numerics defaulted to 0.

## 2. ZMQ execution ticket — Receiver PUSH :5555 → Worker PULL
```python
ticket = {
    "signal_id":    f"{base_id}",          # unique id (timestamp+ticker)
    "ticker":       payload.ticker,         # canonical; Worker maps to broker symbol
    "timestamp_ms": payload.timestamp_ms,
    "entry":        payload.entry,
    "sl":           geometry["out_sl"],
    "tp":           geometry["out_tp"],
    "sl_pips":      payload.sl_pips,
    "signal":       geometry["direction"],  # the signal direction as received
    "lots":         geometry["lots"],
    "order_type":   "market",
}
```
JSON, PUSH on :5555, fire-and-forget; confirm fill via `order_status`.

## 3. ZMQ REP queries — Receiver REQ :5556 → Worker (timeout 3s)
(reference `_worker_core.py:1480`). Request = JSON with `query` (default `equity`) + params.

| `query` | Params | Reply |
|---|---|---|
| `equity` | `ticker`, `want_fee` | `balance`,`equity`,`profit`, contract info (`contract_size`,`trade_tick_size`,`trade_tick_value`,`digits`), `account_currency`, `usd_to_acct_rate`, `trade_allowed`; +fee iff `want_fee` |
| `positions` | — | open positions list |
| `order_status` | `signal_id` | stored execution result |
| `order_check` | `ticker`,`signal`,`lots`,`sl`,`tp` | `verdict` ∈ {ok,reject,transient}, margin |
| `deal_pnl` | `symbol`,`ticket` | `gross`/`commission`/`swap`/`net`, `found` bool |
| `account_mode` | — | demo/real/contest/unknown |
| `checksymbols` | — | per-broker SUPPORTED/FOUND/MISSING |
| `reset_fee_anchor` | — | re-anchors per-cycle fee (fee→0); fire after `/changepropfirm`,`/phase1`,`/phase2` |
| `set_parameters` | floor etc. | pushes the static-DD floor to the Worker (on `/changepropfirm`,`/phase1`) |

**Critical carry-overs:** `deal_pnl` matches strictly by `position_id`+`DEAL_ENTRY_OUT`, window
`to = now + 1 day` (MT5 `deal.time` is **server-tz, not UTC**); `found=False` → caller shows `(est.)`.
Fee: `trading_fee = (balance − Σ all deal.profit) − fee_anchor`, same window, gated to `want_fee` only.

## 4. `config/account_config.json` (single source of truth)
The reference `propfirm_config.json` 12 fields + a phase block + flags. **Raw firm limits are entered via
the `/changepropfirm` wizard; the Receiver applies buffers (`02 §4`) before saving the effective values.**
```jsonc
{
  // --- challenge limits (RAW as entered; effective values after buffers live in memory/derived) ---
  "profit_target_pct":         10.0,
  "max_drawdown_overall_pct":  5.0,     // K2 (no buffer)
  "max_drawdown_daily_pct":    3.0,     // K1 (buffered −1 → enforce 2)
  "consistency_threshold_pct": 30.0,    // K5 (buffered −1 → 29)
  "min_profit_days":           3,
  "daily_profit_cap_pct":      2.5,     // K3 = profit_target×0.25 (auto)
  "baseline_equity":           100000.0,// immutable anchor; NEVER from live MT5
  "day_start_equity":          100000.0,// live balance at wizard completion; resets at day_roll
  "day_start_date_utc":        null,
  "propfirm_day_roll":         "11:00", // SGT firm reset; /setdayroll to match "Resets In"
  "initial_deposit":           100000.0,// actual capital; reporting/% only; zero effect on sizing

  // --- phase + risk ---
  "phase":                     1,
  "phase1": { "stages": [], "active_stage_index": 0, "profitable_days": 0,
              "last_stage_day": null, "first_reward": 0.0, "fixed_risk": 0.0, "max_lots": 0.0 },
  "risk_pct":                  0.01,    // Phase 2 sizing
  "modes": { "conservative": {"risk_pct": 0.01}, "aggressive": {"risk_pct": 0.02} },  // optional toggle
  "active_mode":               "conservative",
  "max_open_positions":        2,
  "trading_window":            { "current": null, "next": null },

  // --- runtime flags ---
  "active":              true,
  "permanently_halted":  false,
  "daily_halted":        false,
  "daily_halted_date":   null,
  "soft_kill_override_day": null
}
```
A separate `consistency_log.json` holds per-day locked profits (Phase 2 K5), reset each cycle.
`symbol_cache_<login>.json`, `fee_anchor_<login>.json`, `dd_floor.json` are per-VPS / gitignored.

## 5. Env (`secrets/.env`)
**Worker:**
```ini
MT5_LOGIN=<account login>          # hard guard: account_info().login must equal this
MT5_TERMINAL_PATH=                 # only if multiple MT5 installs
ZMQ_PULL_BIND=tcp://0.0.0.0:5555
ZMQ_REP_BIND=tcp://0.0.0.0:5556
FIREBASE_JOURNAL_ENABLED=true      # MUST be 'true' or the close watcher never starts (silent death)
FIREBASE_JOURNAL_DRY_RUN=false
GOOGLE_APPLICATION_CREDENTIALS=secrets/firebase-service-account.json
```
**Receiver:**
```ini
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat id>
ZMQ_PUSH_CONNECT=tcp://<worker_ip>:5555
ZMQ_REQ_CONNECT=tcp://<worker_ip>:5556
NEWS_WINDOW=<minutes>
FINNHUB_TOKEN=<token>
```
> Journaling silent-death guard: log a WARNING on Worker startup if `FIREBASE_JOURNAL_ENABLED`≠`true` or
> `FIREBASE_JOURNAL_DRY_RUN`=`true`. Re-set after any `.env` rebuild or journaling dies silently.
