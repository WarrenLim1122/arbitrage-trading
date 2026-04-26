# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: Automated Trade Execution Engine (TEE)

A four-layer cross-hedging dual-account system. The personal Fusion Markets account follows the signal direction; the FundingPips prop firm account simultaneously executes the **inverse direction** as a hedge. Position sizing is phase-dependent and remotely controlled via Telegram.

## Architecture

```
TradingView (15m chart — one chart per pair)
  └── layer0/signal_engine.pine
        │  [HTTP POST webhook → public internet]
  layer1/main.py          (VPS #1, Linux, port 8000 — public)
        │  [internal HTTP POST]
  layer2/logic_core.py    (VPS #1, Linux, port 8001 — internal)
        │  [ZeroMQ PUSH → across network]
        ├── layer3/worker_prop.py      (VPS #2, Windows, ZeroMQ PULL)
        └── layer3/worker_personal.py  (VPS #3, Windows, ZeroMQ PULL)

Telegram Bot API ←→ layer2/logic_core.py   (phase control + prop firm config + error alerts)
```

## Infrastructure (Live as of 2026-04-24)

| VPS | Provider | IP | OS | Purpose | Cost |
|---|---|---|---|---|---|
| VPS #1 | DigitalOcean (SGP1) | 152.42.213.98 | Ubuntu 24.04 | Layer 1 + Layer 2 + nginx + TLS | $18/month |
| VPS #2 | Vultr | 45.76.156.55 | Windows Server | worker-prop (prop firm MT5) | ~$15–20/month |
| VPS #3 | Vultr | 139.180.136.233 | Windows Server | worker-personal (personal MT5) | ~$15–20/month |

- **Public HTTPS endpoint**: https://api.warrenlimzf.com/signal (nginx + Let's Encrypt TLS)
- **Telegram bot name**: HedgeHog (bot token in VPS #1 `.env`)
- **VPS #2 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=88dfe741-382d-47fe-a19c-199baa534bfc`
- **VPS #3 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=6288e88e-1ad6-468a-a584-914bd04590b1`
- **Billing**: DigitalOcean charges card at end of month. Vultr runs on prepaid credit (Visa ending 7119 auto-charges when low).

---

## Build Status

| Layer | Files | Status |
|---|---|---|
| 0 — Signal Engine | `layer0/signal_engine.pine`, `signal_engine_backtest.pine` | ✅ LIVE — 9 alerts active on TradingView |
| 1 — Gatekeeper | `layer1/main.py`, `layer1/news_filter.py` | ✅ LIVE — systemd on VPS #1 |
| 2 — Logic Core | `layer2/logic_core.py` | ✅ LIVE — systemd on VPS #1 |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ LIVE — PowerShell on VPS #2 + #3 |

**Current phase**: Gate D — 7-day demo run started 2026-04-25. Target go-live: ~2026-05-03.

**Important**: VPS #1 Layer 1 and Layer 2 run as systemd services (auto-restart on crash). VPS #2 and #3 workers run in PowerShell windows — if the VPS reboots, the workers must be manually restarted. Do NOT close the PowerShell window on VPS #2/#3; closing the noVNC browser tab is safe.

---

## Covered Instruments

9 pairs. Any other ticker is rejected at Layer 1.

```
EURUSD  GBPUSD  USDCHF  USDCAD  USDJPY  NZDUSD  XAUUSD  XAGUSD  NAS100
```

`pip_type` in webhook:
- `"jpy"` — USDJPY
- `"index"` — NAS100
- `"standard"` — all others (EURUSD, GBPUSD, USDCHF, USDCAD, NZDUSD, XAUUSD, XAGUSD)

Symbol map (`config/symbol_map.json`): NAS100 → USTEC (MetaQuotes broker name).

---

## Immutable Risk Math

These rules never change between Phase 1 and Phase 2.

**Directional logic:**

| Signal | Personal Account | Prop Firm |
|---|---|---|
| LONG | LONG (follows signal) | SHORT (inverse) |
| SHORT | SHORT (follows signal) | LONG (inverse) |

**RR per account (immutable):**

Both accounts share the same RR (0.27) — they mirror each other in opposite directions.
TP is taken from the signal (payload.tp) for personal; prop mirrors it symmetrically across entry.

**Lot sizing sequence:**

```
# Single SL distance — both accounts use the same reference from the signal
sl_distance = abs(entry − payload.sl)

Step A — Prop dollar risk (uses BASELINE equity, not live equity)
  prop_dollar_risk = baseline_equity × 0.0067

Step B — Prop lots (from prop contract data)
  prop_lots = prop_dollar_risk / ((sl_distance / prop_point) × prop_tick_value)

Step C — Personal lots (phase ratio applied to prop lots directly)
  phase_ratio = 0.20 (Phase 1)  |  0.70 (Phase 2)
  pers_lots   = prop_lots × phase_ratio
```

**TP/SL computed by Layer 2:**

```
LONG signal:
  personal: LONG   sl = payload.sl  (last_ltf_sl, below entry)
                   tp = payload.tp  (from signal, entry + sl_distance × 0.27)
  prop:     SHORT  sl = entry + sl_distance  (mirror above entry)
                   tp = 2 × entry − payload.tp  (mirror below entry)

SHORT signal:
  personal: SHORT  sl = payload.sl  (last_ltf_sh, above entry)
                   tp = payload.tp  (from signal, entry − sl_distance × 0.27)
  prop:     LONG   sl = entry − sl_distance  (mirror below entry)
                   tp = 2 × entry − payload.tp  (mirror above entry)
```

**Phase definitions:**

| Phase | Meaning | Phase Ratio |
|---|---|---|
| 1 | Prop firm Evaluation (Not Funded) | 0.20 |
| 2 | Prop firm Funded | 0.70 |

Only the phase ratio changes. Direction and prop sizing are identical in both phases. RR is 0.27 for both accounts (mirrored).

---

## Layer 0 — Signal Engine (`layer0/signal_engine.pine`, Pine Script v6)

**Timeframe**: 15-minute chart. One chart per instrument. 9 charts total.

**HTF (1-Day) — Sticky Trend:**
- `request.security("D", ...)`, pivot N=2 bars each side.
- Tracks 3 most recent 1D highs/lows (ph1/ph2/ph3, pl1/pl2/pl3) via `ta.valuewhen`.
- Bullish: `ph1>ph2>ph3` AND `pl1>pl2>pl3` → `htf_trend = 1`
- Bearish: `ph1<ph2<ph3` AND `pl1<pl2<pl3` → `htf_trend = -1`
- **Sticky**: mixed structure holds previous trend — prevents false reversals during corrections.

**LTF (15-Minute) — Swing Detection:**
- Pivot N=6 bars each side. Tracks `last_ltf_sh` / `last_ltf_sl` with HH/LH/HL/LL labels.
- `long_fired` / `short_fired` reset on each new confirmed pivot.

**Entry triggers:**
- Long: 15m bar closes strictly above `last_ltf_sh` while 1D is bullish.
- Short: 15m bar closes strictly below `last_ltf_sl` while 1D is bearish.
- One signal per breakout (`alert.freq_once_per_bar_close`).

**Price coordinates sent in webhook:**
- Long: `entry = close`, `sl = last_ltf_sl`, `tp = entry + risk × 0.27` (personal TP reference only)
- Short: `entry = close`, `sl = last_ltf_sh`, `tp = entry − risk × 0.27`

**Visuals**: HH/LH/HL/LL labels, signal markers, entry/SL/TP lines + boxes. Diagnostic table (top-right): locked 1D trend, raw structure, 15m SH/SL, TF check.

**Webhook JSON payload:**
```json
{
  "signal":           "LONG",
  "ticker":           "EURUSD",
  "timestamp_ms":     1714000000000,
  "timeframe":        "15m",
  "entry":            1.08500,
  "sl":               1.08300,
  "tp":               1.08554,
  "sl_pips":          20.0,
  "sl_percent":       0.1852,
  "rr_ratio":         0.27,
  "order_type":       "MARKET",
  "daily_trend":      "BULLISH",
  "m15_swing_high":   1.08490,
  "m15_swing_low":    1.08300,
  "pip_type":         "standard"
}
```

**Pine Script v6 known fixes (do not revert):**
- `long_json` and `short_json` must be single-line strings — multi-line string concatenation causes CE10156.
- `alertcondition()` removed entirely — it requires a `const string` but JSON contains series values (CE10123). `alert()` inside `if` blocks is sufficient for webhook delivery.

`layer0/signal_engine_backtest.pine` — same logic with `strategy()` for TradingView Strategy Tester.

**TradingView alert settings (all 9 alerts):**
- Condition: Any alert() function call
- Expiration: Open-ended
- Timeframe: 15m
- Webhook URL: https://api.warrenlimzf.com/signal
- Alerts are global — not tied to any layout. They fire from TradingView servers independently.

---

## Layer 1 — Gatekeeper (`layer1/main.py`, FastAPI)

- Port 8000, public-facing behind nginx + TLS.
- Validates ticker against 9 allowed pairs (EURUSD, GBPUSD, USDCHF, USDCAD, USDJPY, NZDUSD, XAUUSD, XAGUSD, NAS100).
- Queries Finnhub (`/calendar/economic`) via `layer1/news_filter.py`.
  - 60-minute in-memory cache.
  - Suppresses signal if any high-impact event for either currency is within ±60 min.
  - NZD, CAD, NAS100 (→ US) are now correctly mapped to Finnhub country codes.
  - `FAIL_OPEN=true` by default.
- Forwards clean signals to Layer 2 via internal HTTP POST.
- Env vars: `FINNHUB_API_KEY`, `LAYER2_URL`, `NEWS_WINDOW_MINUTES`, `NEWS_FAIL_OPEN`.

---

## Layer 2 — Logic Core (`layer2/logic_core.py`, Python)

### Telegram Bot Commands

| Command | Description |
|---|---|
| `/emergency` | **Nuclear button** — force-close ALL positions on both MT5 accounts immediately + halt |
| `/changepropfirm` | 9-step wizard — collects raw prop firm limits, auto-applies buffers, saves config |
| `/consistency` | Show Phase 2 daily profit breakdown and consistency rule status |
| `/propfirm` | Display current prop firm config |
| `/equity` | Query live balance + equity from both MT5 accounts on demand |
| `/positions` | Show all open positions on both accounts (ticker, direction, lots, entry, SL, TP, P&L) |
| `/pnl` | Today's P&L vs daily profit cap and drawdown limits |
| `/health` | Ping all 4 layers and report live/dead status |
| `/news` | Upcoming high-impact events in the next 4 hours for all covered pairs |
| `/suppressed` | Active suppression blackboard — pairs currently blocked and why |
| `/closepair EURUSD` | Close all positions for a pair on both accounts + block new signals until /resumepair |
| `/resumepair EURUSD` | Unblock a pair closed with /closepair |
| `/setmaxpos 2` | Set max simultaneous open trades (1–10). Default 2. If >5, warns about daily DD exposure |
| `/maxpos` | Show current position limit and current open count |
| `/phase1` | Set phase ratio ×0.20, runs /changepropfirm first, locks baseline equity from live MT5 |
| `/phase2` | Next phase wizard — shows Phase 1 settings, ask same/different, locks new baseline |
| `/stop` | Halt signal processing (open trades continue to their SL/TP naturally) |
| `/resume` | Resume (blocked if profit target reached — requires `/phase2` first) |
| `/status` | Phase, active state, max positions, SGT curfew, equity snapshots |
| `/cancel` | Cancel wizard mid-flow |

**`/stop` vs `/emergency`:**
- `/stop` — stops new signals only. Open positions keep running to SL/TP. Use when pausing.
- `/emergency` — stops new signals AND immediately force-closes all open positions on both accounts. Use when something is wrong and you need to exit the market right now.

Chat ID lock: commands from any other Telegram user are silently ignored.

### Trade Notification (automatic)

Every time a signal is successfully dispatched to both workers, a Telegram message is sent with:
- Ticker, direction, lots, entry, SL, TP for both prop and personal accounts
- Dollar risk for each account
- Phase and baseline equity

### /changepropfirm Wizard (9 steps)

On-demand utility — only needed when switching prop firms, starting a new challenge, or resetting baseline equity. The config in `propfirm_config.json` persists across all restarts and can run unchanged for months.

Asks for the firm's **raw** values. Buffers are applied automatically before saving:

| Input | Firm's raw | Buffer applied | Enforced at |
|---|---|---|---|
| Max DD Daily % | e.g. 3% | −1 pp always | 2% |
| Max DD Overall % | e.g. 6% | no buffer | 6% |
| Profit Target % | e.g. 10% | none | 10% |
| Daily Profit Cap | computed internally | `profit_target × 0.25` | 2.5% |
| Consistency Threshold % | e.g. 30% | use 29% (1% safety buffer) | 29% |

`drawdown_is_static` and `raw_spread_account` must be `true`. If either is entered as `false`/`no`/`dynamic`, the wizard warns and requires explicit `CONFIRM` before accepting — both are flagged in the review summary.

On confirmation: fetches live equity from MT5 prop worker and stores as `baseline_equity`. Config saved to `config/propfirm_config.json`.

### Equity Monitoring Thread (30 s interval)

Queries **prop firm worker equity only** via ZMQ REQ/REP. All kill conditions are evaluated exclusively against the prop firm account — the personal account's P&L is never checked. Daily kills are measured from `day_start_equity`, which resets at **11:00 SGT each day** (matching the prop firm's own daily reset timer).

| # | Phase | Basis | Condition | Action |
|---|---|---|---|---|
| Kill 1 | All | Daily from `day_start_equity` | daily loss ≥ `max_drawdown_daily_pct` (2%) | FORCE_CLOSE both + halt |
| Kill 2 | All | Overall from `baseline_equity` | overall loss ≥ `max_drawdown_overall_pct` | FORCE_CLOSE both + **permanent halt** |
| Kill 3 | All | Daily from `day_start_equity` | daily profit ≥ `daily_profit_cap_pct` (2.5%) | FORCE_CLOSE both + halt |
| Kill 4 | All | Overall from `baseline_equity` | overall profit ≥ `profit_target_pct` (10%) | FORCE_CLOSE both + **permanent halt** → /phase2 to continue |
| Kill 5 | **Phase 2 only** | Consistency log | `max_day_profit / total_profit < consistency_threshold_pct` (default 29%) AND ≥2 profitable days | FORCE_CLOSE both + **permanent halt** → submit payout claim → /phase2 + /resume |

**Kill 5 — Consistency Rule details:**
- Tracks daily profits in `config/consistency_log.json` during Phase 2. Each day's profit is locked in at the 11:00 SGT reset.
- Today's live P&L (including open positions) is included so positions exit the moment the rule is satisfied mid-day.
- When triggered: Telegram sends structured table of daily breakdown + prompt to submit payout claim.
- Log resets on each `/phase2` confirm (new funded cycle).
- `consistency_threshold_pct` is configurable per firm (asked in `/changepropfirm` wizard, default 29%). A 29% threshold with a 2.5% daily cap guarantees the rule is met around ~8.6% total profit — before Kill 4's 10% target.

When a kill fires: pushes `{"action": "FORCE_CLOSE", "reason": "..."}` to both ZMQ PUSH sockets + Telegram alert.

**Worker health monitoring**: if either worker fails to respond for 3 consecutive 30s checks (~90s), a Telegram alert fires with instructions to restart the worker. Recovery is also alerted.

**Position mismatch monitoring (every 30s, within equity monitor thread):**

Every cycle (when both workers are online), Layer 2 queries open positions from both accounts and compares them:

| Mismatch type | Condition | Action |
|---|---|---|
| `prop_only` | Ticker open on prop, missing on personal for ≥30s | Close orphan on prop + Telegram alert |
| `pers_only` | Ticker open on personal, missing on prop for ≥30s | Close orphan on personal + Telegram alert |
| `same_direction` | Both accounts hold same direction on same ticker | Close on BOTH accounts + Telegram alert (hedge broken) |

Correct state: prop holds direction X → personal holds the OPPOSITE direction on same ticker. A 30s grace period avoids false alarms during normal execution time. If the mismatch resolves itself within 30s, no action is taken.

**Max open positions gate (inline in `/signal` endpoint):**

Before dispatching any signal, Layer 2 queries prop worker position count. Prop is the authoritative count (1 signal = 1 prop position). If `count ≥ max_open_positions`, the signal is rejected with `max_positions_reached`. Default limit: 2. Configurable via `/setmaxpos`. Slots free immediately when a position closes — no cooldown.

### SGT Curfew Gate (inline in `/signal` endpoint)

- Signals 00:00–11:59 SGT or Saturday/Sunday: rejected, no state change to `active`.
- At curfew transition: monitor thread dispatches FORCE_CLOSE with `halt=False` — positions closed, `active` flag untouched. Trading resumes automatically at 12:00 SGT on next weekday.
- **Trading window: 12:00–00:00 SGT, weekdays only** (12 hours per day). Chosen so the 11:00 SGT prop-firm daily reset is safely behind the open before trading begins.

### Signal Processing Sequence

1. SGT curfew gate.
2. Check `active`, `permanently_halted`.
3. Max open positions gate — query prop position count, reject if ≥ limit.
4. Query `prop_equity + contract data` from prop worker via ZMQ.
5. Query `contract data` from personal worker via ZMQ.
6. Calculate lots per the immutable risk math (prop from dollar risk; personal = prop_lots × phase_ratio).
7. Compute prop SL/TP (mirror of signal) and personal SL/TP (from signal directly).
8. Dispatch two ZMQ PUSH tickets with all computed values.
9. Send trade notification to Telegram.

**News Pre-Close Monitor (60s interval, background thread):**

Runs independently of signal flow. Two-stage design using ForexFactory data (no API key):

| Window | Time to event | Action |
|---|---|---|
| Awareness | 31–60 min before | Log only — no position close, no suppression |
| Ban | 0–30 min before event + 0–30 min after | One-time: close ticker positions + suppress new entries |

- Deduped by `(ticker, event_utc.isoformat())` — fires exactly once per event per pair.
- Suppression ends 30 min after the event (`suppression_end = event_utc + 30 min`).
- `CLOSE_TICKER` dispatched to both workers; `NEWS_SUPPRESS` sent so Layer 3 rejects new tickets.
- `NEWS_CLEAR` sent when suppression window expires (checked every 60s).
- Sends Telegram alert on ban-zone trigger naming event, pair, and suppression end time.

`CLOSE_TICKER`, `NEWS_SUPPRESS`, `NEWS_CLEAR` are handled in Layer 3's PULL loop and bypass the dormant guard.

Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (Finnhub no longer used — ForexFactory is public).

---

## Layer 3 — Execution Workers (`layer3/_worker_core.py`)

Shared logic in `_worker_core.py`; wrappers `worker_prop.py` and `worker_personal.py` set `WORKER_NAME` and `MT5_MAGIC`.

**Three threads per worker:**
- PULL thread (main): receives execution tickets and FORCE_CLOSE messages.
- REP thread (daemon): answers equity + contract data queries from Layer 2.
- SGT scheduler thread (daemon): manages `_dormant` flag, force-closes at curfew transition.

### Pip Value — XAUUSD vs Forex

```python
def _pip_value(ticker: str) -> float:
    info = mt5.symbol_info(ticker)
    # XAUUSD: 2 decimal places → 1 pip = 1 tick (no ×10)
    # Forex (5 dp) and JPY (3 dp): 1 pip = 10 ticks
    if ticker == "XAUUSD":
        return info.trade_tick_value
    return info.trade_tick_value * 10.0
```

Slippage pip sizes: USDJPY = 0.01, XAUUSD = 0.01, all others = 0.0001.

### Order Execution

- `deviation=20` points on every market order.
- Filling mode auto-detected per symbol (IOC → FOK → RETURN), cached per symbol.
- Retriable errors (requote, price changed, price off): max 3 retries, 0.5 s delay.
- Latency logged: `receipt_ms → sent_ms → fill_ms`, `slippage_pips`.
- `_mt5_lock` serialises all MT5 calls across all threads.

### FORCE_CLOSE Handler

PULL loop: `{"action": "FORCE_CLOSE"}` bypasses dormant guard — always executes immediately. `_force_close_all(reason)` iterates `mt5.positions_get()`, closes each with a market order, logs per-position result.

### SGT Scheduler

- Dormant: 00:00–08:59 SGT weekdays + all day Saturday/Sunday.
- Active → dormant transition: calls `_force_close_all("sgt_curfew")` once per calendar day.
- PULL loop drops execution tickets while dormant; FORCE_CLOSE bypasses.

Env vars: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `ZMQ_PULL_ADDR`, `ZMQ_REP_ADDR`, `MT5_MAGIC`.

---

## Config Files

| File | Key fields | Changed by |
|---|---|---|
| `config/phase_config.json` | `phase`, `active`, `permanently_halted`, `max_open_positions`, `last_signal_ts` | Telegram commands |
| `config/propfirm_config.json` | All 12 propfirm fields | `/changepropfirm` wizard only — never edit manually |
| `config/risk_params.json` | `prop_risk_pct`, `phase_multipliers`, `layer3_zmq` | Manual edit only |
| `config/symbol_map.json` | Ticker → broker symbol mapping (e.g. NAS100 → USTEC) | Manual edit only |

### `config/propfirm_config.json` fields

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

`baseline_equity` and `day_start_equity` are populated live from MT5 by the wizard — never set manually. Never edit this file manually — use `/changepropfirm`.

---

## Toolchain

| Tool | Purpose |
|---|---|
| `uv` + `pyproject.toml` | Package manager |
| FastAPI + uvicorn | Layers 1 and 2 HTTP |
| httpx | Async HTTP (Layer 1→2) + sync Telegram alerts from monitor thread |
| pyzmq | ZeroMQ sockets (Layer 2→3) |
| python-telegram-bot | Telegram bot in Layer 2 |
| MetaTrader5 | Layer 3 only — Windows VPS |
| tzdata | Layer 3 Windows only — `zoneinfo` SGT timezone support |
| Finnhub REST API | Economic calendar in Layer 1 |

---

## Running Each Layer

```bash
# VPS #1 (Linux) — base deps
uv sync

# VPS #2 and #3 (Windows) — Layer 3 deps
uv sync --extra layer3

# Layer 1 — Gatekeeper
uvicorn layer1.main:app --host 127.0.0.1 --port 8000

# Layer 2 — Logic Core
uvicorn layer2.logic_core:app --host 127.0.0.1 --port 8001

# Layer 3 — Workers (one per Windows VPS)
uv run python layer3/worker_prop.py
uv run python layer3/worker_personal.py
```

---

## Deploying Code Changes to VPS

**Which VPSes to update depends on which layer changed:**

| Layer changed | VPS #1 (Linux) | VPS #2 worker-prop | VPS #3 worker-personal |
|---|---|---|---|
| Layer 1 or 2 | ✅ git pull + restart | ❌ | ❌ |
| Layer 3 | ❌ | ✅ git pull + restart | ✅ git pull + restart |
| config/ files | ✅ git pull + restart | ✅ git pull + restart | ✅ git pull + restart |
| Layer 0 (Pine Script) | ❌ | ❌ | ❌ (TradingView only) |

`uv sync --extra layer3` is only needed on VPS #2/#3 when `pyproject.toml` changed. For pure code changes, `git pull` + restart is enough.

---

### Step 1 — Push from Mac

```bash
git add <changed files>
git commit -m "description"
git push
```

### Step 2 — Update VPS #1 (SSH from Mac terminal)

```bash
ssh root@152.42.213.98
cd /root/arbitrage-trading
git pull
sudo systemctl restart layer2   # if Layer 2 changed
sudo systemctl restart layer1   # if Layer 1 changed
systemctl status layer2         # verify running
```

### Step 3 — Update VPS #2 worker-prop (noVNC browser console)

```powershell
cd C:/arbitrage
git pull
uv sync --extra layer3          # only if pyproject.toml changed
# Stop running worker: Ctrl+C
uv run python layer3/worker_prop.py
```

### Step 4 — Update VPS #3 worker-personal (noVNC browser console)

```powershell
cd C:/arbitrage
git pull
uv sync --extra layer3          # only if pyproject.toml changed
# Stop running worker: Ctrl+C
uv run python layer3/worker_personal.py
```

---

### Key facts

- Repo path on VPS #1: `/root/arbitrage-trading`
- Layer 2 runs as systemd service: `layer2.service` — always restart with `sudo systemctl restart layer2`
- Layer 1 runs as systemd service: `layer1.service` — always restart with `sudo systemctl restart layer1`
- VPS #2 noVNC: `https://console.vultr.com/subs/vps/novnc/?id=88dfe741-382d-47fe-a19c-199baa534bfc`
- VPS #3 noVNC: `https://console.vultr.com/subs/vps/novnc/?id=6288e88e-1ad6-468a-a584-914bd04590b1`
- `&&` does not work in PowerShell — run commands one at a time
- noVNC clipboard: use the clipboard icon on the left sidebar, paste into the box, then right-click in PowerShell to paste
- Workers on VPS #2/#3 run in PowerShell — do NOT close the PowerShell window. Closing the noVNC browser tab is safe.

---

## Telegram — Updating TELEGRAM_CHAT_ID (personal → group)

The bot ignores messages from any chat ID not in `.env`. To move the bot to a Telegram group:

**Step 1 — Stop Layer 2 on VPS #1** (so the bot stops consuming updates):
```bash
ssh root@152.42.213.98
sudo systemctl stop layer2
```

**Step 2 — Send a message to the group** (from your phone or Telegram desktop):
- Add @HedgeHog_TEEBot to the group (or it should already be a member).
- Send any message in the group (e.g. "hello").

**Step 3 — Fetch the update to get the group chat ID**:
```bash
# Still in the VPS #1 SSH session:
source /root/arbitrage-trading/.env
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -m json.tool | grep '"id"'
```
Group chat IDs are **negative** numbers (e.g. `-1001234567890`). Use the one under `"chat"`.

**Step 4 — Update .env and restart**:
```bash
OLD_ID=$(grep TELEGRAM_CHAT_ID /root/arbitrage-trading/.env | cut -d= -f2)
NEW_ID=-1001234567890          # replace with your actual group chat ID
sed -i "s/TELEGRAM_CHAT_ID=${OLD_ID}/TELEGRAM_CHAT_ID=${NEW_ID}/" /root/arbitrage-trading/.env
sudo systemctl start layer2
systemctl status layer2        # verify running
```

**Step 5 — Verify**: Send `/status` in the group. The bot should reply with system status.

**Note**: The bot's `CHAT_ID` lock means commands from any other chat are silently ignored — the group ID must match exactly.

---

## Hard Constraints

- **Personal account always trades inverse direction.**
- **MetaTrader5 import on Linux = instant failure.** Layers 1 and 2 must never import it.
- **Prop firm config is wizard-only.** Never edit `propfirm_config.json` manually.
- **Phase switching is Telegram-only.**
- **Lot sizing uses baseline_equity × 0.67%, not live equity.** This keeps sizing stable regardless of open trade P&L.
- **Personal lots = prop lots × phase ratio.** `pers_lots = prop_lots × phase_ratio`. Both accounts use the same SL distance from the signal. Do NOT compute personal lots from a separate dollar risk / pip value formula.
- **VPS #2 and #3 must have distinct public IPs.**
- **ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open** between VPS #1 and VPS #2/#3.
- **TradingView Premium** required for webhook alert delivery.
- **One TradingView chart per instrument** — 9 charts total for 9 pairs.
- **Demo-first mandatory**: full pipeline on paper/demo MT5 for ≥7 trading days before live capital.

---

## SGT Time Reference

| SGT | UTC | Event |
|---|---|---|
| 00:00 SGT | 16:00 UTC (prev day) | Curfew begins — force-close all positions |
| 11:00 SGT | 03:00 UTC | Prop firm daily reset — day_start_equity resets, completed day profit locked to consistency log |
| 12:00 SGT | 04:00 UTC | Trading resumes (weekdays only) |
| Saturday 00:00 SGT | Friday 16:00 UTC | Weekend dormant begins |
| Monday 12:00 SGT | Monday 04:00 UTC | Weekend dormant ends |

---

## Deployment Gates

```
Gate 0 — CONFIRM WITH USER before any deployment:
  [x] Verify prop firm daily reset time: currently hardcoded at 11:00 SGT in _propfirm_day()
      — Confirmed with FundingPips demo account. Verify again on live account.

Gate A — before Layer 1 goes live (VPS #1):
  [x] FINNHUB_API_KEY in .env
  [x] nginx installed, TLS certificate (certbot), reverse proxy to port 8000
  [x] TradingView Premium — webhook URL set to https://api.warrenlimzf.com/signal
  [x] uv sync complete, uvicorn starts cleanly

Gate B — before Layer 2 Telegram bot goes live (VPS #1):
  [x] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
  [x] /status command returns correct state
  [x] /phase1 confirmation message received
  [x] /resume activates signal processing
  [x] /changepropfirm wizard completed — baseline_equity = 100,000 (demo)

Gate C — before Layer 3 workers go live (VPS #2 and VPS #3):
  [x] VPS #2 and #3 provisioned (Windows Server), distinct public IPs
  [x] MT5 installed and logged in on each VPS
  [x] MT5 → Tools → Options → Expert Advisors → "Allow automated trading" checked
  [x] Firewall: VPS #2 and #3 accept ZMQ ports 5555–5556 from VPS #1 IP only
  [x] uv sync --extra layer3 (installs MetaTrader5 + tzdata)
  [x] ZMQ connection test: Layer 2 equity query returns balance from both workers
  [x] config/risk_params.json updated with actual ZMQ URLs and VPS IPs

Gate D — mandatory before live capital (≥7 trading days on demo, started 2026-04-25):
  [ ] Phase 1 ratio (×0.20) verified on ≥10 signals end-to-end
  [ ] Phase 2 ratio (×0.70) verified on ≥10 signals
  [ ] Inverse direction confirmed on personal account for every signal
  [ ] XAUUSD pip value verified — lots must be ~10× smaller than equivalent forex trade
  [ ] News filter tested: ≥3 high-impact suppressions logged correctly
  [ ] Latency audit: receipt_ms → fill_ms < 500ms on all orders
  [ ] Telegram error alerts tested by intentionally crashing Layer 3
  [ ] Trade notification fires correctly on every dispatch
  [ ] /equity command returns live balance from both workers
  [ ] /emergency closes all positions on both accounts immediately
  [ ] Kill 1 (daily loss): drain demo equity past daily DD → FORCE_CLOSE fires on BOTH accounts + Telegram alert
  [ ] Kill 3 (daily profit cap, Phase 2): simulate +cap% in one day → FORCE_CLOSE fires + Telegram alert
  [ ] Kill 4 (Phase 1 target): hit overall profit target → permanent halt confirmed, /phase2 + /resume required
  [ ] SGT midnight curfew: open position at 23:59 SGT → force-closed by 00:01 SGT
  [ ] SGT 09:00 resume: first signal after 09:00 SGT dispatched normally
  [ ] Weekend rejection: signal arriving Saturday/Sunday returns "weekend" rejection
  [ ] FORCE_CLOSE propagates to BOTH MT5 accounts simultaneously
```

---

## Go-Live Checklist (after Gate D passes, ~2026-05-03)

1. Log into MT5 on VPS #2 — switch to real **FundingPips** credentials
2. Log into MT5 on VPS #3 — switch to real **Fusion Markets** credentials
3. Send `/changepropfirm` in Telegram — re-run wizard with real FundingPips limits, baseline locks to live balance
4. Send `/phase1` then `/resume`
5. Verify first live signal dispatches correctly and trade appears in both MT5 accounts

---

## Session Continuity (read this after /clear)

All four layers are code-complete and fully deployed. The system is in Gate D — 7-day demo run.

**Current state (as of 2026-04-25):**
- Layer 0: 9 alerts active on TradingView. Webhooks firing and delivering successfully.
- Layer 1: Live at https://api.warrenlimzf.com/signal. Rejecting signals during SGT curfew (correct). Finnhub news filter active.
- Layer 2: Running on VPS #1. Telegram bot (HedgeHog) active. `/equity`, `/emergency`, trade notifications all implemented. 4 kill conditions active.
- Layer 3: Both workers running on VPS #2 (prop, MetaQuotes demo) and VPS #3 (personal, MetaQuotes demo account 106260846).

**Nothing left to build. Monitor Gate D checklist items as signals fire.**

**What to do next:**
1. Wait for signals during trading hours (09:00–00:00 SGT, weekdays only)
2. On each signal: check Telegram for trade notification, verify MT5 positions match (prop = signal direction, personal = inverse)
3. Tick off Gate D checklist items as they occur
4. Go live ~2026-05-03 using the Go-Live Checklist above
