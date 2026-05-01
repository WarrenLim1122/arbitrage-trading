# CLAUDE.md

Guidance for Claude Code. For full technical details — layer deep-dives, risk math, Telegram commands, kill conditions, deployment gates, go-live checklist — read **`TECHNICAL.md`**.

## Workflow Rules

- **Auto-push to GitHub after every code change.** Warren has given standing permission for all pushes to `main`. Never wait to be reminded — commit and push immediately after making any file edits.
- **Always include full deployment steps** in responses after a push: exact SSH command, `git pull`, and restart commands for every affected VPS, ready to copy-paste.

### Deployment steps after every push

**VPS #1 — Layer 1 or 2 changes:**
```bash
ssh root@152.42.213.98
cd /root/arbitrage-trading && git pull && sudo systemctl restart layer2
systemctl status layer2
```
Restart `layer1` instead if only Layer 1 changed; restart both if both changed.

**VPS #2 (Personal — worker_personal.py) and VPS #3 (Prop — worker_prop.py) — Layer 3 changes (noVNC PowerShell):**

Warren's workflow — always write steps this way:
1. Close the PowerShell window with the **X button** (kills the worker — Warren cannot type Ctrl+C in noVNC)
2. Open a new PowerShell window
3. Run one at a time:
```
cd C:\arbitrage
git pull
uv run python layer3/worker_personal.py
```
Use `worker_prop.py` for VPS #3. `uv sync --extra layer3` only if `pyproject.toml` changed.

---

## Project

Automated Trade Execution Engine — 4-layer cross-hedging system. Personal account (Fusion Markets) follows signal direction; prop firm account (FundingPips) executes the **inverse** as a hedge. Sizing is phase-dependent, controlled via Telegram.

## Architecture

```
TradingView (15m chart — one chart per pair)
  └── layer0/signal_engine.pine
        │  [HTTPS webhook]
  layer1/main.py          (VPS #1, port 8000 — public)
        │  [internal HTTP]
  layer2/logic_core.py    (VPS #1, port 8001 — internal)
        │  [ZeroMQ PUSH]
        ├── layer3/worker_personal.py  (VPS #2, Windows)
        └── layer3/worker_prop.py      (VPS #3, Windows)
Telegram Bot API ←→ layer2/logic_core.py
```

## Infrastructure

| VPS | Provider | IP | OS | Purpose |
|---|---|---|---|---|
| VPS #1 | DigitalOcean (SGP1) | 152.42.213.98 | Ubuntu 24.04 | Layer 1 + Layer 2 + nginx + TLS |
| VPS #2 | Vultr | 139.180.136.233 | Windows Server | worker-personal (Fusion Markets MT5) |
| VPS #3 | Vultr | 45.76.156.55 | Windows Server | worker-prop (FundingPips MT5) |

- **Public endpoint**: https://api.warrenlimzf.com/signal
- **Telegram bot**: HedgeHog (token in VPS #1 `.env`)
- **VPS #2 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=6288e88e-1ad6-468a-a584-914bd04590b1`
- **VPS #3 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=88dfe741-382d-47fe-a19c-199baa534bfc`
- **Billing**: DigitalOcean end-of-month. Vultr prepaid credit (Visa 7119 auto-charges).

## Build Status

| Layer | Files | Status |
|---|---|---|
| 0 — Signal Engine | `layer0/signal_engine.pine` | ✅ LIVE — 8 alerts active, `in_trade` gate deployed 2026-04-27. **Frozen — do not edit without asking Warren first.** |
| 1 — Gatekeeper | `layer1/main.py`, `layer1/news_filter.py`, `layer1/ff_calendar.py` | ✅ LIVE — systemd on VPS #1 |
| 2 — Logic Core | `layer2/logic_core.py`, `layer2/telegram_handlers.py`, `layer2/state.py` | ✅ LIVE — all features below deployed 2026-05-01 |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ LIVE — PowerShell on VPS #2 + #3 |

**Current phase**: Gate D — 7-day demo run started 2026-04-25. Target go-live: ~2026-05-03.

VPS #1 layers run as systemd services (auto-restart). VPS #2/#3 workers run in PowerShell — must be manually restarted after VPS reboot. Do NOT close the PowerShell window; closing the noVNC browser tab is safe.

## Covered Instruments

8 pairs — any other ticker rejected at Layer 1:
```
EURUSD  GBPUSD  USDCHF  USDCAD  USDJPY  NZDUSD  XAUUSD  XAGUSD
```
`pip_type`: `"jpy"` for USDJPY, `"standard"` for all others.

---

## Kill Condition Math (static baseline — critical to understand)

All kill thresholds are calculated against `baseline_equity` (the locked starting balance), never against live equity or day-start equity. This means every % threshold converts to a fixed dollar amount for the entire evaluation.

| Kill | Trigger condition | Formula |
|---|---|---|
| K1 — Daily loss | `(day_start − equity) / baseline × 100 ≥ max_drawdown_daily_pct` | Always −$2,000 if DD=2% and baseline=$100k |
| K2 — Overall loss | `(baseline − equity) / baseline × 100 ≥ max_drawdown_overall_pct` | Fixed floor, e.g. $94,000 if DD=6% |
| K3 — Daily profit cap | `(equity − day_start) / baseline × 100 ≥ daily_profit_cap_pct` | Always +$2,500 if cap=2.5% and baseline=$100k |
| K4 — Profit target | `equity ≥ baseline × (1 + profit_target_pct / 100)` | Fixed ceiling |
| K5 — Consistency | largest day / total profit < consistency_threshold_pct (Phase 2 only) | e.g. firm says 30% → stored as 29% → fires when largest day < 29% |

`daily_profit_cap_pct` is auto-set to `profit_target_pct × 0.25` (25% of target, enforcing before the 30% consistency threshold).
`max_drawdown_daily_pct` enforced after −1pp buffer (e.g. firm says 3% → bot triggers at 2%).
`consistency_threshold_pct` also buffered −1pp automatically (e.g. firm says 30% → enter 30 → stored/enforced at 29%).
`/phase1` is idempotent — re-running it mid-evaluation does NOT overwrite an existing baseline.

---

## Trading Window

- Stored in `config/trading_window.json` — `current_window` (start/end HH:MM SGT) and `next_window` (optional, applied at 11:00 SGT session rollover).
- Default: 12:00–00:00 SGT weekdays. `00:00` end = midnight (treated as 1440 minutes internally).
- Change via `/setwindow HH:MM HH:MM` Telegram command — choose "today" (immediate) or "tomorrow" (next rollover).
- `_is_sgt_curfew()` reads from `_trading_window` dict dynamically — no restart needed after `/setwindow`.
- Weekends always curfew regardless of window setting.
- **`00:00` is ambiguous — handled by `is_end` flag in `_window_minutes(t_str, is_end=False)`**: as a start time `00:00` = 0 min; as an end time `00:00` = 1440 min (midnight). Without this, setting a 24-hour window (`00:00–00:00`) would cause permanent curfew because start and end would both resolve to 1440. Always pass `is_end=True` when calling `_window_minutes` for the end time.
- **Layer 3 has NO time-of-day curfew of its own.** The `/setwindow` window in Layer 2 is the sole gate for execution hours. Layer 3's `_sgt_scheduler` only sets `_dormant = True` on weekends (`weekday >= 5`). Any time-of-day logic in `_sgt_scheduler` must not be re-added — it caused EXECUTION FAILURE spam (Layer 2 dispatched, Layer 3 silently dropped, 5s check found no positions). Fixed 2026-05-01.
- **`/status` Active vs Curfew are independent**: "Status: 🟢 Active" means the trading engine is armed (not halted). "Curfew: Yes — dormant" means the current time is outside the window. Both can be true simultaneously — Active + Curfew = engine ready but window closed, no trades until window opens.

---

## Known MT5 Gotchas (operational — read before touching Layer 3)

- **"Disable algorithmic trading when the account has been changed"** (MT5 → Tools → Options → Expert Advisors) must be **unchecked** on both VPS #2 and VPS #3. If checked, MT5 silently disables algo trading after any account change — orders are rejected with no error in Layer 3. Root cause of the 2026-04-24 NZDUSD silent failure. Uncheck once; it persists.
- **`trade_allowed` monitoring**: equity monitor reads this flag from both workers every 30s via ZMQ REP. Immediate Telegram alert if MT5 auto-disables algo trading, with step-by-step fix instructions.
- **5-second position verification**: after every signal dispatch, Layer 2 waits 5s, queries actual positions from both workers, and sends "✅ Trade Opened" or "⚠️ Execution Issue" with the exact error. No more silent failures.
- **XAGUSD lot sizing**: use `trade_tick_size` (0.0001), NOT `point` (0.001). Using `point` inflates lots 10×. Fixed 2026-04-22.
- **MetaTrader5 import on Linux = instant crash.** Layers 1 and 2 must never import it.
- **Price display must use `_fmt_price(symbol, price)` from `state.py`** — MT5 returns floats with binary precision artifacts (e.g. `1.3498700000000001`). `_fmt_price` rounds to the correct decimal places per instrument: JPY pairs = 3dp, XAUUSD = 2dp, XAGUSD = 4dp, all others = 5dp. Every SL/TP/entry price shown in Telegram alerts goes through this helper. Any new price display code must use it too.

- **Close detection buffer**: when one leg of a hedge closes before the other (e.g. personal SL hits one poll before prop TP), the close is held in `_pending_closes` for up to 120 s. A single combined alert fires only after both legs confirm closed or the buffer expires. This prevents duplicate split alerts and false orphan force-closes. Root cause of the session 5 split-alert incident: legs were ~2 min apart; 30 s buffer was too short.
- **Mismatch grace period**: position mismatches must persist ≥120 s (`grace = 120`) before CRITICAL MISMATCH fires. Matches the close buffer so a normal staggered close doesn't trigger a false mismatch alert.
- **MT5 account identity**: every equity reply from Layer 3 includes `login` (int) and `server` (str) from `mt5.account_info()`. Layer 2 caches the last-seen logins in `_propfirm` as `live_prop_login` / `live_pers_login`. All account-specific Telegram alerts (trade opened, trade closed, /equity, /positions, /baseline, mismatch, worker offline) display `MT5: {login}` and `Worker: VPS #N / worker_xxx`. Do not remove these fields — they are the sole runtime indicator of which MT5 account is actually connected.
- **Account mismatch gate**: before executing any signal, Layer 2 compares live MT5 logins against `expected_prop_login` / `expected_pers_login` (stored in `_propfirm`). If set and mismatched, the signal is rejected with HTTP 503 and a Telegram alert. Set expected logins via `/setpropaccount <login>` and `/setpersonalaccount <login>`. Check with `/accountcheck`.
- **Personal account baseline** (`pers_baseline_equity`) is manual-only — set via `/setpersonalbaseline <amount>`. `_update_pers_day_start()` only writes `pers_day_start_equity`; it never touches the baseline. The baseline was previously auto-set from the live MT5 balance ($10,042.75 instead of the correct $10,000) — that bug is fixed. Never auto-write `pers_baseline_equity`.
- **News stale cache fallback**: if ForexFactory calendar fetch returns empty (API down), `ff_calendar.py` returns the last good cache instead of an empty list. Prevents false "all clear" news state.
- **News suppression clear notification**: when a news suppression window expires, a grouped 🔴→🟢 Telegram alert fires (listing all pairs cleared at once) before dispatching `NEWS_CLEAR` to Layer 3. `/news` shows 🟠 per event; `/blackboard` shows 🔴 per suppressed pair.
- **`dd_floor.json` stale value on VPS #3**: Layer 3 prop worker loads `config/dd_floor.json` at startup. Layer 2 only sends `SET_PARAMETERS` (which updates this file) on explicit events (`/phase1`, `/changepropfirm` wizard). If the worker restarts with a stale/wrong floor, STATIC DD GUARD fires every 30s and blocks all trades until Layer 2 resends. Fix: run `/phase1` in Telegram (idempotent) to trigger a resend. Root cause of the 2026-04-30 incident: previous incorrect baseline entry ($1,234,567) had saved floor=$1,160,492.98. Never enter test/placeholder numbers as `baseline_equity` in the wizard.

---

## Hard Constraints

- Personal account always trades **opposite** direction to prop firm.
- Lot sizing uses `baseline_equity × 0.67%` — never live equity.
- Personal lots = `prop_lots × phase_ratio`. Never compute from a separate dollar risk formula.
- Prop firm config: wizard-only (`/changepropfirm`). Never edit `propfirm_config.json` manually.
- **`baseline_equity` is immutable** — only written by explicit user commands: `/changepropfirm` wizard (Step 10/10), `/phase1` (only when baseline is 0), `/phase2` wizard. `_update_day_start()` NEVER touches it — only `day_start_equity` and `day_start_date_utc`. Nothing automatic can overwrite it. `/setbaseline` command does NOT exist — was removed; re-run wizard Step 10/10 to correct baseline.
- **`pers_baseline_equity` is manual-only** — only written by `/setpersonalbaseline <amount>`. `_update_pers_day_start()` only writes `pers_day_start_equity`. Never auto-set from live MT5 balance.
- Phase switching: Telegram-only (`/phase1`, `/phase2`).
- ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open between VPS #1 and VPS #2/#3.
- TradingView Premium required for webhook delivery.
- One TradingView chart per instrument — 8 charts, 8 pairs (NAS100 removed).
- Demo-first mandatory: ≥7 trading days before live capital.

---

## Current State (as of 2026-05-01, session 5)

All four layers deployed and operational. Gate D demo run in progress. Target go-live ~2026-05-03.

- Layer 0: 8 alerts active. `in_trade` gate live. **Signal engine is frozen — do not touch.**
- Layer 1: Live. News filter active. Stale-cache fallback on FF calendar failure deployed.
- Layer 2: Full feature set deployed:
  - Kill 1/2/3/4/5 use static `baseline_equity` divisor — all thresholds are fixed dollar amounts
  - K1 layered floors from baseline (staircase). K2 hard floor safety net. K3 daily cap from day_start. K4 cumulative profit target. K5 consistency (Phase 2 only).
  - `/phase1` is idempotent — will not overwrite an existing baseline mid-evaluation
  - 120 s close-detection buffer and 120 s mismatch grace — prevents split alerts and false CRITICAL MISMATCH when legs close ~2 min apart
  - Position Closed alert: title = 🟢 Take Profit / 🔴 Stop Loss based on Personal P&L; sections: Personal Signal (VPS #2), Prop Hedge (VPS #3), After Close, Equity
  - Trade Opened alert: "✅ Trade Opened" on success, "⚠️ Execution Issue" on failure; sections: Personal Signal (VPS #2), Prop Hedge (VPS #3), Context
  - `/equity`: Floating P&L row added (from `acct.profit`); Balance label removed; Personal (VPS #2) shown first, Prop (VPS #3) second; Today/Overall P&L per account
  - `/baseline`: new command — shows live MT5 balance + overall P&L for both accounts
  - `/setpersonalbaseline <amount>`: only way to set `pers_baseline_equity` — never auto-set
  - `/setpropaccount <login>` / `/setpersonalaccount <login>`: register expected MT5 login IDs
  - `/accountcheck`: live query both workers, compare expected vs actual login IDs, show server name
  - Account mismatch gate: signal execution blocked (HTTP 503 + Telegram alert) if live MT5 login ≠ expected
  - All account-specific alerts show `MT5: {login}` and `Worker: VPS #N / worker_xxx`
  - Dynamic trading window: `config/trading_window.json` + `/setwindow` Telegram command. Currently set to 12:00–00:00 SGT.
  - `trade_allowed` monitoring + 5 s position verification
  - News suppression clear notification: grouped 🔴→🟢 Telegram alert on window expiry
  - `/news` shows 🟠 per event; `/blackboard` shows 🔴 per pair, 🟢 when clear
  - `/changepropfirm` wizard (10 steps, `/back` supported): buffer rules explicit in each prompt:
    - Step 3/10 (Overall DD): NO auto-buffer — enter firm's exact stated limit
    - Step 4/10 (Daily DD): system subtracts −1pp — enter firm's raw stated value
    - Step 9/10 (Consistency): system subtracts −1pp — enter firm's raw stated value
  - `_apply_buffers()` buffers all three: daily DD, consistency (both −1pp), daily cap (25% of target)
  - `phase_configs["1"]` stores raw wizard values; `_propfirm` stores buffered effective values — no double-buffer on Phase 2
  - All hardcoded `29.0` consistency defaults removed — fully dynamic from wizard input
  - `/cancel` outside a wizard replies "No active wizard to cancel." (no silent failure)
  - All SL/TP/entry prices in Telegram alerts use `_fmt_price(symbol, price)` — no more float artifacts
  - `_window_minutes` fixed: `00:00` as start = 0 min, as end = 1440 min (24h window now works correctly)
  - `/setbaseline` command removed — use `/changepropfirm` Step 10/10 to set prop baseline
- Layer 3: Both workers running (VPS #2 personal account 106497299 on 139.180.136.233, VPS #3 prop account 106496748 on 45.76.156.55, both MetaQuotes demo). Hard-coded `h < 12` curfew removed 2026-05-01 — Layer 3 is only dormant on weekends; trading hours controlled entirely by Layer 2 `/setwindow`. Equity replies include `login`, `server`, and `profit` (floating P&L) fields.

**Next action**: Wait for signals 12:00–00:00 SGT weekdays. Run `/setpropaccount` and `/setpersonalaccount` to arm the mismatch gate. Check Telegram for trade confirmations.
