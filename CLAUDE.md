# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: Automated Trade Execution Engine (TEE)

A four-layer cross-hedging dual-account system. The FundingPips prop firm account executes the primary directional trade; the personal Fusion Markets account simultaneously executes the **inverse direction** as a hedge. Position sizing is phase-dependent and remotely controlled via Telegram.

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

## Build Status

| Layer | Files | Status |
|---|---|---|
| 0 — Signal Engine | `layer0/signal_engine.pine`, `signal_engine_backtest.pine` | COMPLETE — needs TradingView setup |
| 1 — Gatekeeper | `layer1/main.py`, `layer1/news_filter.py` | COMPLETE — needs VPS #1 + nginx |
| 2 — Logic Core | `layer2/logic_core.py` | COMPLETE — needs VPS #1 |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | COMPLETE — needs VPS #2 + #3 (Windows) |

**Telegram bot**: token obtained, chat ID confirmed.
**Next action**: backtest validation on TradingView, then VPS provisioning.

---

## Covered Instruments

6 pairs. Any other ticker is rejected at Layer 1.

```
EURUSD  GBPUSD  AUDUSD  USDCHF  USDJPY  XAUUSD
```

`pip_type` in webhook: `"jpy"` for USDJPY, `"standard"` for all others (including XAUUSD).

---

## Immutable Risk Math

These rules never change between Phase 1 and Phase 2.

**Directional logic:**

| Signal | Prop Firm | Personal Account |
|---|---|---|
| LONG | LONG | SHORT (inverse) |
| SHORT | SHORT | LONG (inverse) |

**RR per account (immutable):**

| Account | RR | TP distance from entry |
|---|---|---|
| Prop Firm | 1/0.27 ≈ 3.7037 | `sl_distance × 3.7037` |
| Personal | 0.27 | `sl_distance × 0.27` (inverse direction) |

**Lot sizing sequence:**

```
sl_distance = abs(entry − sl)           # from webhook

Step A — Prop dollar risk
  prop_dollar_risk = prop_equity × 0.0067

Step B+C — Personal dollar risk
  phase_ratio      = 0.20 (Phase 1)  |  0.70 (Phase 2)
  pers_dollar_risk = prop_dollar_risk × phase_ratio

Step D — Lots (each account uses its own broker's pip value)
  prop_lots = prop_dollar_risk / (sl_pips × prop_pip_value)
  pers_lots = pers_dollar_risk / (sl_pips × pers_pip_value)
```

**TP / personal SL computed by Layer 2 (not taken from webhook):**

```
LONG signal:
  prop_tp  = entry + sl_distance × 3.7037
  pers_sl  = m15_swing_high          # swing high above entry = SHORT stop
  pers_tp  = entry − sl_distance × 0.27

SHORT signal:
  prop_tp  = entry − sl_distance × 3.7037
  pers_sl  = m15_swing_low           # swing low below entry = LONG stop
  pers_tp  = entry + sl_distance × 0.27
```

**Phase definitions:**

| Phase | Meaning | Phase Ratio |
|---|---|---|
| 1 | Prop firm Evaluation (Not Funded) | 0.20 |
| 2 | Prop firm Funded | 0.70 |

Only the phase ratio changes. Direction, RR, and prop sizing are identical in both phases.

---

## Layer 0 — Signal Engine (`layer0/signal_engine.pine`, Pine Script v6)

**Timeframe**: 15-minute chart. One chart per instrument.

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
  "rr_ratio":         0.27,
  "order_type":       "MARKET",
  "daily_trend":      "BULLISH",
  "m15_swing_high":   1.08490,
  "m15_swing_low":    1.08300,
  "pip_type":         "standard"
}
```

`layer0/signal_engine_backtest.pine` — same logic with `strategy()` for TradingView Strategy Tester.

---

## Layer 1 — Gatekeeper (`layer1/main.py`, FastAPI)

- Port 8000, public-facing behind nginx + TLS.
- Validates ticker against 6 allowed pairs (EURUSD, GBPUSD, AUDUSD, USDCHF, USDJPY, XAUUSD).
- Queries Finnhub (`/calendar/economic`) via `layer1/news_filter.py`.
  - 60-minute in-memory cache.
  - Suppresses signal if any high-impact event for either currency is within ±30 min.
  - `FAIL_OPEN=true` by default.
- Forwards clean signals to Layer 2 via internal HTTP POST.
- Env vars: `FINNHUB_API_KEY`, `LAYER2_URL`, `NEWS_WINDOW_MINUTES`, `NEWS_FAIL_OPEN`.

---

## Layer 2 — Logic Core (`layer2/logic_core.py`, Python)

### Telegram Bot Commands

| Command | Description |
|---|---|
| `/changepropfirm` | 8-step wizard — collects raw prop firm limits, auto-applies buffers, saves config |
| `/propfirm` | Display current prop firm config |
| `/phase1` | Set phase ratio ×0.20 |
| `/phase2` | Set phase ratio ×0.70, clears permanent halt |
| `/stop` | Halt signal processing |
| `/resume` | Resume (blocked if Phase 1 target reached — requires `/phase2` first) |
| `/status` | Phase, active state, SGT curfew, equity snapshots |
| `/cancel` | Cancel wizard mid-flow |

Chat ID lock: commands from any other Telegram user are silently ignored.

### /changepropfirm Wizard (8 steps)

On-demand utility — only needed when switching prop firms, starting a new challenge, or resetting baseline equity. The config in `propfirm_config.json` persists across all restarts and can run unchanged for months.

Asks for the firm's **raw** values. Buffers are applied automatically before saving:

| Input | Firm's raw | Buffer applied | Enforced at |
|---|---|---|---|
| Max DD Daily % | e.g. 3% | −1 pp always | 2% |
| Max DD Overall % | e.g. 6% | −1 pp always | 5% |
| Profit Target % | e.g. 10% | none | 10% |
| Daily Profit Cap | computed internally | `profit_target × 0.25` | 2.5% |

The buffer formula is dynamic — give any new firm's raw numbers and the correct enforced values are calculated automatically.

`drawdown_is_static` and `raw_spread_account` must be `true`. If either is entered as `false`/`no`/`dynamic`, the wizard warns and requires explicit `CONFIRM` before accepting — both are flagged in the review summary.

On confirmation: fetches live equity from MT5 prop worker and stores as `baseline_equity`. Config saved to `config/propfirm_config.json`.

### Equity Monitoring Thread (30 s interval)

Queries **prop firm worker equity only** via ZMQ REQ/REP. All kill conditions are evaluated exclusively against the prop firm account — the personal account's P&L is never checked. All kills are **daily P&L only** — measured from `day_start_equity`, which resets at **11:00 SGT each day** (matching the prop firm's own daily reset timer). Overall prop firm drawdown is intentionally not monitored: if the prop firm loses overall, the personal account gains on the inverse, which is the strategy working as designed.

| # | Phase | Basis | Condition | Action |
|---|---|---|---|---|
| Kill 1 | All | Daily from `day_start_equity` | daily loss ≥ `max_drawdown_daily_pct` (2%) | FORCE_CLOSE both + halt |
| Kill 2 | Phase 2 | Daily from `day_start_equity` | daily profit ≥ `daily_profit_cap_pct` (2.5%) | FORCE_CLOSE both + halt |
| Kill 3 | Phase 1 | Overall from `baseline_equity` | overall profit ≥ `profit_target_pct` (10%) | FORCE_CLOSE both + **permanent halt** |

`max_drawdown_overall_pct` is stored in config for reference but does not trigger any kill — the cross-hedge means overall prop drawdown = personal account profit. When a kill fires: pushes `{"action": "FORCE_CLOSE", "reason": "..."}` to both ZMQ PUSH sockets + Telegram alert.

### SGT Curfew Gate (inline in `/signal` endpoint)

- Signals 00:00–08:59 SGT or Saturday/Sunday: rejected, no state change to `active`.
- At curfew transition: monitor thread dispatches FORCE_CLOSE with `halt=False` — positions closed, `active` flag untouched. Trading resumes automatically at 09:00 SGT on next weekday.

### Signal Processing Sequence

1. SGT curfew gate.
2. Check `active`, `phase1_permanently_halted`.
3. Query `prop_equity + prop_pip_value` from prop worker via ZMQ.
4. Query `pers_pip_value` from personal worker via ZMQ.
5. Calculate lots per the immutable risk math above (prop and personal independently).
6. Compute prop TP (1/0.27 RR) and personal SL + TP (0.27 RR, inverse).
7. Dispatch two ZMQ PUSH tickets with all computed values.

Env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

---

## Layer 3 — Execution Workers (`layer3/_worker_core.py`)

Shared logic in `_worker_core.py`; wrappers `worker_prop.py` and `worker_personal.py` set `WORKER_NAME` and `MT5_MAGIC`.

**Three threads per worker:**
- PULL thread (main): receives execution tickets and FORCE_CLOSE messages.
- REP thread (daemon): answers equity + pip value queries from Layer 2.
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
| `config/phase_config.json` | `phase`, `active`, `phase1_permanently_halted`, `last_signal_ts` | Telegram commands |
| `config/propfirm_config.json` | All 12 propfirm fields | `/changepropfirm` wizard only — never edit manually |
| `config/risk_params.json` | `prop_risk_pct`, `phase_multipliers`, `layer3_zmq` | Manual edit only |

### `config/propfirm_config.json` fields

```json
{
  "propfirm_name":            "FundingPips",
  "profit_target_pct":        10.0,
  "max_drawdown_overall_pct": 5.0,
  "max_drawdown_daily_pct":   2.0,
  "drawdown_is_static":       true,
  "raw_spread_account":       true,
  "profit_sharing_pct":       80.0,
  "min_profit_days":          3,
  "daily_profit_cap_pct":     2.5,
  "baseline_equity":          0.0,
  "day_start_equity":         0.0,
  "day_start_date_utc":       "2026-04-23"
}
```

Post-buffer values (firm's raw → enforced): daily DD 3%→2%, overall DD 6%→5%, profit cap 3%→2.5%. `daily_profit_cap_pct` = `profit_target_pct × 0.25`. `baseline_equity` and `day_start_equity` are populated live from MT5 by the wizard — never set manually. Never edit this file manually — use `/changepropfirm`.

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
python layer3/worker_prop.py
python layer3/worker_personal.py
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

---

## Hard Constraints

- **Personal account always trades inverse direction.**
- **MetaTrader5 import on Linux = instant failure.** Layers 1 and 2 must never import it.
- **Prop firm config is wizard-only.** Never edit `propfirm_config.json` manually.
- **Phase switching is Telegram-only.**
- **Lot sizing uses the Capital × 0.67% × Ratio formula.** `pers_lots = prop_lots × ratio` is wrong — personal dollar risk is computed first, then converted to lots using the personal broker's own pip value.
- **VPS #2 and #3 must have distinct public IPs.**
- **ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open** between VPS #1 and VPS #2/#3.
- **TradingView Premium** required for webhook alert delivery.
- **One TradingView chart per instrument** — 6 charts total for 6 pairs.
- **Demo-first mandatory**: full pipeline on paper/demo MT5 for ≥7 trading days before live capital.

---

## SGT Time Reference

| SGT | UTC | Event |
|---|---|---|
| 00:00 SGT | 16:00 UTC (prev day) | Curfew begins — force-close all positions |
| 09:00 SGT | 01:00 UTC | Trading resumes (weekdays only) |
| Saturday 00:00 SGT | Friday 16:00 UTC | Weekend dormant begins |
| Monday 09:00 SGT | Monday 01:00 UTC | Weekend dormant ends |

---

## Deployment Gates

```
Gate 0 — CONFIRM WITH USER before any deployment:
  [ ] Verify prop firm daily reset time: currently hardcoded at 11:00 SGT in _propfirm_day()
      — Check prop firm dashboard "Resets In" timer on two separate days to confirm it is
        always 11:00 SGT (fixed) and does not shift (e.g. tied to a DST timezone).
      — If the reset time differs, update the hour threshold in logic_core.py:_propfirm_day()
        before proceeding.

Gate A — before Layer 1 goes live (VPS #1):
  [ ] FINNHUB_API_KEY in .env
  [ ] nginx installed, TLS certificate (certbot), reverse proxy to port 8000
  [ ] TradingView Premium — webhook URL set to https://<domain>/signal
  [ ] uv sync complete, uvicorn starts cleanly

Gate B — before Layer 2 Telegram bot goes live (VPS #1):
  [ ] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env
  [ ] /status command returns correct state
  [ ] /phase1 confirmation message received
  [ ] /resume activates signal processing
  [ ] (optional) /changepropfirm wizard — only if starting a fresh challenge or switching firms

Gate C — before Layer 3 workers go live (VPS #2 and VPS #3):
  [ ] VPS #2 and #3 provisioned (Windows Server 2022), distinct public IPs
  [ ] MT5 installed and logged in on each VPS
  [ ] MT5 → Tools → Options → Expert Advisors → "Allow automated trading" checked
  [ ] Firewall: VPS #2 and #3 accept ZMQ ports 5555–5556 from VPS #1 IP only
  [ ] uv sync --extra layer3 (installs MetaTrader5 + tzdata)
  [ ] ZMQ connection test: Layer 2 equity query returns balance from both workers
  [ ] config/risk_params.json updated with actual ZMQ URLs and VPS IPs

Gate D — mandatory before live capital (≥7 trading days on demo):
  [ ] Phase 1 ratio (×0.20) verified on ≥10 signals end-to-end
  [ ] Phase 2 ratio (×0.70) verified on ≥10 signals
  [ ] Inverse direction confirmed on personal account for every signal
  [ ] XAUUSD pip value verified — lots must be ~10× smaller than equivalent forex trade
  [ ] News filter tested: ≥3 high-impact suppressions logged correctly
  [ ] Latency audit: receipt_ms → fill_ms < 500ms on all orders
  [ ] Telegram error alerts tested by intentionally crashing Layer 3
  [ ] /changepropfirm wizard: all 8 fields accepted, buffered values shown correctly
  [ ] Kill 1 (daily loss): drain demo equity past daily DD → FORCE_CLOSE fires on BOTH accounts + Telegram alert
  [ ] Kill 2 (daily profit cap, Phase 2): simulate +cap% in one day → FORCE_CLOSE fires + Telegram alert
  [ ] Kill 3 (Phase 1 target): hit overall profit target → permanent halt confirmed, /phase2 + /resume required
  [ ] SGT midnight curfew: open position at 23:59 SGT → force-closed by 00:01 SGT
  [ ] SGT 09:00 resume: first signal after 09:00 SGT dispatched normally
  [ ] Weekend rejection: signal arriving Saturday/Sunday returns "weekend" rejection
  [ ] FORCE_CLOSE propagates to BOTH MT5 accounts simultaneously
```

---

## Session Continuity (read this after /clear)

All four layers are code-complete. Nothing left to write.

**Current state:**
- Layer 0: 15m LTF + 1D HTF sticky trend. `signal_engine.pine` (live) and `signal_engine_backtest.pine` (backtest) both complete.
- Layer 1: 6-pair filter (EURUSD, GBPUSD, AUDUSD, USDCHF, USDJPY, XAUUSD) + Finnhub news filter.
- Layer 2: Correct lot sizing — `prop_dollar_risk = prop_equity × 0.0067`, `pers_dollar_risk = prop_dollar_risk × phase_ratio`, each account converts to lots using its own broker pip value. Prop TP = 1/0.27 RR. Personal TP = 0.27 RR inverse. Personal SL from `m15_swing_high`/`m15_swing_low`.
- Layer 3: XAUUSD pip value fix (`trade_tick_value` only, no ×10). All kill switches operational via FORCE_CLOSE dispatch from Layer 2.

**What to do next — in order:**

1. **Backtest** — open `signal_engine_backtest.pine` in TradingView Strategy Tester on 15m chart across all pairs. Check signal frequency and drawdown profile. If signals are too sparse, relax `N_HTF` from 2 to 1.
2. **VPS provisioning** — Gate A + C. Fill `<VPS2_IP>` and `<VPS3_IP>` in `config/risk_params.json`. Open firewall ports 5555–5556 on VPS #2/#3.
3. **Start Telegram bot** — `/phase1` to set phase, `/resume` to activate. That's all that's required for normal operation.
4. **7-day demo run** — Gate D in full before any live capital.
5. **Go live** — switch MT5 to live accounts, `/phase1`, `/resume`. Run `/changepropfirm` only if you need to update baseline equity for the live account.
