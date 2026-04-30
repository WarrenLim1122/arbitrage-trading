# TECHNICAL.md

Full technical reference for the Automated Trade Execution Engine. Read this file when working on any specific layer, the risk math, or deployment procedures.

---

## Immutable Risk Math

These rules never change between phases.

**Directional logic:**

| Signal | Personal Account | Prop Firm |
|---|---|---|
| LONG | LONG (follows signal) | SHORT (inverse) |
| SHORT | SHORT (follows signal) | LONG (inverse) |

**RR per account:** Personal follows signal exactly (SL/TP from webhook). Funded is exact inverse: SL = signal TP, TP = signal SL. RR of 0.27 is baked into Layer 0 — Layer 2 does not recompute it.

**Lot sizing sequence:**

```
tp_distance = abs(payload.tp − entry)    # funded SL distance
sl_distance = abs(entry − payload.sl)    # personal SL distance

Step A — Prop dollar risk (BASELINE equity, not live)
  prop_dollar_risk = baseline_equity × 0.0067

Step B — Funded lots
  prop_lots = prop_dollar_risk / ((tp_distance / trade_tick_size) × trade_tick_value)

  CRITICAL: use trade_tick_size NOT point.
  XAGUSD on MetaQuotes: point=0.001, trade_tick_size=0.0001. Using point = 10× bug.

  Equivalent shortcut (USD-denominated pairs only):
    prop_lots = prop_dollar_risk / (tp_distance × contract_size)
    XAGUSD: $670 / (0.277 × 5000) = 0.48 lots ✓
    XAUUSD: $670 / (SL_dist × 100)
    Forex:  $670 / (SL_dist × 100000)
    Does NOT work for USDJPY/USDCHF/USDCAD (SL in foreign currency).

  Example EURUSD: tp=0.00054, tick_size=0.00001, tick_value=$1 → $54/lot → 12.41 lots
  Example XAGUSD: tp=0.277,   tick_size=0.0001,  tick_value=$0.5 → $1,385/lot → 0.48 lots

Step C — Personal lots
  phase_ratio = 0.20 (Phase 1) | 0.70 (Phase 2)
  pers_lots   = prop_lots × phase_ratio
```

**TP/SL assignment:**
```
personal: sl = payload.sl,  tp = payload.tp
funded:   sl = payload.tp,  tp = payload.sl   (exact swap)

Example BUY (entry=1.08500, sl=1.08300, tp=1.08554):
  personal BUY:  sl=1.08300  tp=1.08554
  funded   SELL: sl=1.08554  tp=1.08300
```

**Phase definitions:**

| Phase | Meaning | Phase Ratio |
|---|---|---|
| 1 | Prop Evaluation | 0.20 |
| 2 | Prop Funded | 0.70 |

---

## Standard Lot Sizes

| Symbol | Lot size |
|---|---|
| EURUSD, GBPUSD, USDCHF, USDCAD, USDJPY, NZDUSD | 100,000 currency units |
| XAUUSD | 100 troy oz |
| XAGUSD | 5,000 troy oz |

---

## Layer 0 — Signal Engine (`layer0/signal_engine.pine`, Pine Script v6)

**Timeframe**: 15m chart. One chart per instrument. 8 charts total.

**HTF (1-Day) — Sticky Trend:**
- `request.security("D", ...)`, pivot N=2 bars each side.
- Tracks 3 most recent 1D highs/lows via `ta.valuewhen`.
- Bullish: `ph1>ph2>ph3` AND `pl1>pl2>pl3` → `htf_trend = 1`
- Bearish: `ph1<ph2<ph3` AND `pl1<pl2<pl3` → `htf_trend = -1`
- **Sticky**: mixed structure holds previous trend.

**LTF (15m) — Swing Detection:**
- Pivot N=6 bars. Tracks `last_ltf_sh` / `last_ltf_sl` with HH/LH/HL/LL labels.
- `long_fired` / `short_fired` reset on each new confirmed pivot.

**In-trade gate** (added 2026-04-27 — prevents double entries):
```pine
var bool  in_trade   = false
var float trade_sl   = na
var float trade_tp   = na
var bool  trade_long = false
// resets when price hits either SL or TP level
```
Mirrors `strategy.position_size == 0` logic.

**Entry triggers:**
- Long: 15m bar closes strictly above `last_ltf_sh` while 1D bullish AND `not in_trade`.
- Short: 15m bar closes strictly below `last_ltf_sl` while 1D bearish AND `not in_trade`.
- `alert.freq_once_per_bar_close`.

**Webhook payload:**
```json
{
  "signal": "LONG", "ticker": "EURUSD", "timestamp_ms": 1714000000000,
  "timeframe": "15m", "entry": 1.08500, "sl": 1.08300, "tp": 1.08554,
  "sl_pips": 20.0, "sl_percent": 0.1852, "rr_ratio": 0.27,
  "order_type": "MARKET", "daily_trend": "BULLISH",
  "m15_swing_high": 1.08490, "m15_swing_low": 1.08300, "pip_type": "standard"
}
```

**Pine Script v6 known fixes (do not revert):**
- JSON strings must be single-line — multi-line concatenation causes CE10156.
- `alertcondition()` removed — requires `const string` but JSON has series values (CE10123). `alert()` inside `if` blocks is sufficient.

`layer0/signal_engine_backtest.pine` — same logic with `strategy()` for Strategy Tester.

**TradingView alert settings (all 8 alerts):**
- Condition: Any alert() function call
- Expiration: Open-ended
- Timeframe: 15m
- Webhook URL: https://api.warrenlimzf.com/signal
- Alerts are global — not tied to any layout. Fire from TradingView servers independently.
- **Updating Pine Script in the editor does NOT affect already-created alerts.**

---

## Layer 1 — Gatekeeper (`layer1/main.py`, FastAPI)

- Port 8000, public-facing behind nginx + TLS.
- Validates ticker against 8 allowed pairs. Any other ticker rejected immediately.
- Queries Finnhub (`/calendar/economic`) via `layer1/news_filter.py`:
  - 60-minute in-memory cache.
  - Suppresses if any high-impact event for either currency is within ±60 min.
  - NZD, CAD mapped to correct Finnhub country codes.
  - `FAIL_OPEN=true` by default (pass signal through if Finnhub unreachable).
- Forwards clean signals to Layer 2 via internal HTTP POST.
- Env vars: `FINNHUB_API_KEY`, `LAYER2_URL`, `NEWS_WINDOW_MINUTES`, `NEWS_FAIL_OPEN`.

---

## Layer 2 — Logic Core (`layer2/logic_core.py`, Python)

### Telegram Bot Commands

| Command | Description |
|---|---|
| `/emergency` | Force-close ALL positions on both accounts immediately + halt |
| `/changepropfirm` | 10-step wizard — collects raw limits, applies buffers, saves config. Step 10 asks for initial account balance (baseline). |
| `/consistency` | Phase 2 daily profit breakdown and consistency rule status |
| `/propfirm` | Display current prop firm config |
| `/equity` | Live balance + equity from both MT5 accounts |
| `/positions` | All open positions on both accounts |
| `/pnl` | Today's P&L vs daily cap and drawdown limits |
| `/health` | Ping all 4 layers |
| `/news` | High-impact events in next 4 hours for all pairs |
| `/blackboard` | Active suppression blackboard |
| `/closepair EURUSD` | Close all positions for pair + block until /resumepair |
| `/resumepair EURUSD` | Unblock a pair |
| `/setmaxpos 2` | Set max simultaneous open trades (1–10, default 2) |
| `/maxpos` | Current position limit and open count |
| `/phase1` | Set phase ratio ×0.20. Only sets baseline if currently 0 (from live MT5 balance). Idempotent — will not overwrite an existing baseline. |
| `/phase2` | Next phase wizard — locks new baseline |
| `/stop` | Halt new signals (open trades continue to SL/TP) |
| `/resume` | Resume signal processing |
| `/status` | Phase, active state, max positions, SGT curfew, equity snapshots |
| `/cancel` | Cancel wizard mid-flow |

**`/stop` vs `/emergency`:** `/stop` halts new signals only. `/emergency` halts AND force-closes all open positions immediately.

### Trade Notification + 5-Second Verification (deployed 2026-04-27)

After dispatch, Layer 2 waits 5 seconds, queries actual positions from both workers, then sends one Telegram message:
- **"Trade Confirmed — TICKER"** with ✅ on both accounts.
- **"⚠️ EXECUTION FAILURE — TICKER"** with ❌ and exact error per account + "ACTION REQUIRED" prompt.

This replaces the old pattern of sending "Trade Fired" before confirming actual execution.

### /changepropfirm Wizard

Buffers applied automatically:

| Input | Buffer | Enforced at |
|---|---|---|
| Max DD Daily % | −1 pp always | 2% (for 3% firm) |
| Max DD Overall % | none | firm's value |
| Profit Target % | none | firm's value |
| Daily Profit Cap | profit_target × 0.25 | 2.5% (for 10% target) |
| Consistency Threshold % | −1 pp | 29% (for 30% firm) |

`drawdown_is_static` and `raw_spread_account` must be `true` — wizard warns and requires CONFIRM if either is false.

### Equity Monitoring Thread (30s interval)

Evaluates all kill conditions against prop firm account only. Daily P&L measured from `day_start_equity`, which resets at **11:00 SGT** (prop firm's daily reset).

| # | Phase | Condition | Action |
|---|---|---|---|
| Kill 1 | All | daily loss ≥ max_drawdown_daily_pct (2%) | FORCE_CLOSE + halt |
| Kill 2 | All | overall loss ≥ max_drawdown_overall_pct | FORCE_CLOSE + permanent halt |
| Kill 3 | All | daily profit ≥ daily_profit_cap_pct (2.5%) | FORCE_CLOSE + halt |
| Kill 4 | All | overall profit ≥ profit_target_pct (10%) | FORCE_CLOSE + permanent halt → /phase2 |
| Kill 5 | Phase 2 | max_day/total < consistency_threshold (29%) AND ≥2 profitable days | FORCE_CLOSE + permanent halt → payout claim |

**`trade_allowed` monitoring (deployed 2026-04-27):** equity monitor reads `trade_allowed` from both workers every 30s. Immediate Telegram alert when MT5 disables algo trading, cleared when restored. Fires once per state change.

**Worker health monitoring:** 3 consecutive timeouts (~90s) → Telegram alert to restart. Recovery also alerted.

**Position mismatch monitoring (every 30s):**

| Type | Condition | Action |
|---|---|---|
| prop_only | Ticker on prop, missing on personal ≥30s | Close orphan on prop |
| pers_only | Ticker on personal, missing on prop ≥30s | Close orphan on personal |
| same_direction | Both accounts same direction | Close on BOTH + alert |

### SGT Curfew Gate

- **Trading window: 12:00–00:00 SGT, weekdays only.**
- Signals 00:00–11:59 SGT or weekends: rejected immediately, no state change.
- At 00:00 SGT: monitor thread dispatches FORCE_CLOSE (halt=False) — positions closed, `active` untouched. Resumes automatically at 12:00 SGT next weekday.

### Signal Processing Sequence

1. SGT curfew gate
2. Check `active`, `permanently_halted`
3. Max open positions gate (query prop count, reject if ≥ limit)
4. Query `prop_equity + contract data` from prop worker (ZMQ REQ)
5. Query `contract data` from personal worker (ZMQ REQ)
6. Calculate lots (prop from dollar risk; personal = prop_lots × phase_ratio)
7. Compute SL/TP for both accounts
8. Dispatch two ZMQ PUSH tickets
9. Launch `_verify_and_notify` as async task (5s wait → confirm → Telegram)

### News Pre-Close Monitor (60s interval)

Uses ForexFactory data (no API key needed).

| Window | Time to event | Action |
|---|---|---|
| Awareness | 31–60 min before | Log only |
| Ban | 0–30 min before + 0–30 min after | Close positions + suppress new entries |

Deduped by `(ticker, event_utc.isoformat())`. `CLOSE_TICKER` + `NEWS_SUPPRESS` dispatched to both workers. When suppression expires: grouped 🔴→🟢 Telegram alert fires first, then `NEWS_CLEAR` sent to workers and pairs removed from blackboard.

Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

---

## Layer 3 — Execution Workers (`layer3/_worker_core.py`)

Shared logic in `_worker_core.py`. `worker_prop.py` and `worker_personal.py` set `WORKER_NAME` and `MT5_MAGIC`.

**Three threads per worker:**
- PULL thread (main): execution tickets + FORCE_CLOSE
- REP thread (daemon): answers equity + contract data + `trade_allowed` queries from Layer 2
- SGT scheduler thread (daemon): manages `_dormant` flag, force-closes at curfew

**REP socket reply includes `trade_allowed`** (added 2026-04-27) — Layer 2 uses this for algo-trading monitoring.

### Layer 3 SGT Schedule

- Dormant: 00:00–08:59 SGT weekdays + all day Saturday/Sunday.
- PULL loop drops execution tickets while dormant; FORCE_CLOSE always bypasses.

### Order Execution

- `deviation=20` points on every market order.
- Filling mode: IOC → FOK → RETURN, auto-detected and cached per symbol.
- Retriable errors (requote, price changed, price off): max 3 retries, 0.5s delay.
- `_mt5_lock` serialises all MT5 calls across threads.

### pip_value (slippage display only — not used for lot sizing)

```python
def _pip_value(ticker: str) -> float:
    info = mt5.symbol_info(ticker)
    if ticker == "XAUUSD":
        return info.trade_tick_value          # 1 pip = 1 tick
    return info.trade_tick_value * 10.0       # 1 pip = 10 ticks (forex + JPY)
```

Slippage pip sizes: USDJPY/XAUUSD = 0.01, all others = 0.0001.

Env vars: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `ZMQ_PULL_ADDR`, `ZMQ_REP_ADDR`, `MT5_MAGIC`.

---

## Config Files

| File | Key fields | Changed by |
|---|---|---|
| `config/phase_config.json` | `phase`, `active`, `permanently_halted`, `max_open_positions` | Telegram commands |
| `config/propfirm_config.json` | All 12 propfirm fields | `/changepropfirm` wizard only |
| `config/risk_params.json` | `prop_risk_pct`, `phase_multipliers`, `layer3_zmq` | Manual edit only |
| `config/symbol_map.json` | Ticker → broker symbol mapping | Manual edit only |

`config/propfirm_config.json` schema:
```json
{
  "propfirm_name":             "FundingPips",
  "profit_target_pct":         10.0,
  "max_drawdown_overall_pct":  5.0,
  "max_drawdown_daily_pct":    2.0,
  "drawdown_is_static":        true,
  "raw_spread_account":        true,
  "profit_sharing_pct":        80.0,
  "min_profit_days":           3,
  "daily_profit_cap_pct":      2.5,
  "consistency_threshold_pct": 29.0,
  "baseline_equity":           100000.0,
  "day_start_equity":          100000.0,
  "day_start_date_utc":        "2026-04-25"
}
```
`baseline_equity` = user-entered initial account balance from wizard Step 10/10. **Never fetched from live MT5.** Immutable for the life of the evaluation — only `/changepropfirm`, `/setbaseline <amount>`, or `/phase1` (when 0) can change it.
`day_start_equity` = live prop MT5 balance at wizard completion; resets daily at 11:00 SGT rollover via `_update_day_start()`. `_update_day_start()` never touches `baseline_equity`.

---

## Toolchain

| Tool | Purpose |
|---|---|
| `uv` + `pyproject.toml` | Package manager |
| FastAPI + uvicorn | Layers 1 and 2 HTTP |
| httpx | Async HTTP (Layer 1→2) + sync Telegram from monitor thread |
| pyzmq | ZeroMQ (Layer 2→3) |
| python-telegram-bot | Telegram bot in Layer 2 |
| MetaTrader5 | Layer 3 only — Windows VPS |
| tzdata | Layer 3 Windows only — `zoneinfo` SGT support |
| Finnhub REST API | Economic calendar in Layer 1 |

---

## Running Each Layer

```bash
# VPS #1 (Linux)
uv sync
uvicorn layer1.main:app --host 127.0.0.1 --port 8000
uvicorn layer2.logic_core:app --host 127.0.0.1 --port 8001

# VPS #2 and #3 (Windows)
uv sync --extra layer3
uv run python layer3/worker_prop.py       # VPS #2
uv run python layer3/worker_personal.py  # VPS #3
```

---

## Deploying Code Changes

| Layer changed | VPS #1 | VPS #2 | VPS #3 |
|---|---|---|---|
| Layer 1 or 2 | ✅ restart | ❌ | ❌ |
| Layer 3 | ❌ | ✅ restart | ✅ restart |
| config/ files | ✅ restart | ✅ restart | ✅ restart |
| Layer 0 (Pine Script) | ❌ | ❌ | ❌ (TradingView only) |

`uv sync --extra layer3` only if `pyproject.toml` changed.

**Key facts:**
- Repo path on VPS #1: `/root/arbitrage-trading`
- `&&` does not work in PowerShell — run commands one at a time
- noVNC clipboard: use the clipboard icon on the left sidebar, paste into the box, then right-click in PowerShell to paste

---

## SGT Time Reference

| SGT | UTC | Event |
|---|---|---|
| 00:00 SGT | 16:00 UTC (prev day) | Curfew — force-close all positions |
| 11:00 SGT | 03:00 UTC | Prop firm daily reset — day_start_equity resets |
| 12:00 SGT | 04:00 UTC | Trading resumes (weekdays only) |
| Saturday 00:00 SGT | Friday 16:00 UTC | Weekend dormant begins |
| Monday 12:00 SGT | Monday 04:00 UTC | Weekend dormant ends |

---

## Deployment Gates

```
Gate 0:
  [x] Prop firm daily reset time confirmed at 11:00 SGT (FundingPips demo). Verify on live account.

Gate A — Layer 1 live:
  [x] FINNHUB_API_KEY in .env
  [x] nginx + TLS (certbot), reverse proxy to port 8000
  [x] TradingView Premium, webhook URL set
  [x] uv sync complete, uvicorn starts cleanly

Gate B — Layer 2 Telegram live:
  [x] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
  [x] /status, /phase1, /resume working
  [x] /changepropfirm wizard completed — baseline_equity = 100,000 (demo)

Gate C — Layer 3 workers live:
  [x] VPS #2 and #3 provisioned, distinct IPs
  [x] MT5 installed and logged in on each VPS
  [x] MT5 → Tools → Options → Expert Advisors → "Allow automated trading" checked
  [x] "Disable algorithmic trading when the account has been changed" UNCHECKED (both VPS)
  [x] Firewall: VPS #2 and #3 accept ZMQ ports 5555–5556 from VPS #1 IP only
  [x] uv sync --extra layer3
  [x] ZMQ connection test: equity query returns balance from both workers

Gate D — demo run (started 2026-04-25, target ≥7 days):
  [ ] Phase 1 ratio (×0.20) verified on ≥10 signals end-to-end
  [ ] Phase 2 ratio (×0.70) verified on ≥10 signals
  [ ] Inverse direction confirmed on personal account for every signal
  [ ] XAUUSD pip value verified — lots ~10× smaller than equivalent forex
  [ ] News filter tested: ≥3 high-impact suppressions logged correctly
  [ ] Latency audit: receipt_ms → fill_ms < 500ms on all orders
  [ ] "Trade Confirmed ✅✅" Telegram fires on every dispatch
  [ ] "⚠️ EXECUTION FAILURE" fires and shows correct error when a worker is down
  [ ] /equity returns live balance from both workers
  [ ] /emergency closes all positions on both accounts immediately
  [ ] Kill 1 (daily loss): drain demo equity past daily DD → FORCE_CLOSE + alert
  [ ] Kill 3 (daily profit cap): simulate +cap% → FORCE_CLOSE + alert
  [ ] Kill 4 (Phase 1 target): hit profit target → permanent halt, /phase2 required
  [ ] SGT curfew: open position at 23:59 SGT → force-closed by 00:01 SGT
  [ ] Weekend rejection: signal on Saturday/Sunday → "weekend" rejection
  [ ] FORCE_CLOSE propagates to BOTH accounts simultaneously
```

---

## Go-Live Checklist (after Gate D, ~2026-05-03)

1. Log into MT5 on VPS #2 — switch to real **FundingPips** credentials
2. Log into MT5 on VPS #3 — switch to real **Fusion Markets** credentials
3. Send `/changepropfirm` — re-run wizard with real FundingPips limits. At Step 10/10 enter the prop firm's stated initial account balance (e.g. `100000`) — this becomes the static `baseline_equity` for all kill calculations
4. Send `/phase1` then `/resume`
5. Verify first live signal dispatches correctly and appears in both MT5 accounts

---

## Telegram — Updating TELEGRAM_CHAT_ID (personal → group)

**Step 1** — Stop Layer 2:
```bash
ssh root@152.42.213.98
sudo systemctl stop layer2
```

**Step 2** — Send any message to the group in Telegram.

**Step 3** — Fetch the group chat ID:
```bash
source /root/arbitrage-trading/.env
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -m json.tool | grep '"id"'
```
Group IDs are negative numbers (e.g. `-1001234567890`). Use the one under `"chat"`.

**Step 4** — Update .env and restart:
```bash
OLD_ID=$(grep TELEGRAM_CHAT_ID /root/arbitrage-trading/.env | cut -d= -f2)
NEW_ID=-1001234567890
sed -i "s/TELEGRAM_CHAT_ID=${OLD_ID}/TELEGRAM_CHAT_ID=${NEW_ID}/" /root/arbitrage-trading/.env
sudo systemctl start layer2
systemctl status layer2
```

**Step 5** — Send `/status` in the group to verify.
