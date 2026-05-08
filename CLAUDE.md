# CLAUDE.md

Guidance for Claude Code. For full technical details — layer deep-dives, risk math, Telegram commands, kill conditions, deployment gates, go-live checklist — read **`TECHNICAL.md`**.

## Workflow Rules

- **Auto-push to GitHub after every code change.** Warren has given standing permission for all pushes to `main`. Never wait to be reminded — commit and push immediately after making any file edits.
- **After a push, tell Warren which Telegram `/update` commands to run** — do not repeat full deployment steps in responses. Routine deployment instructions now live inside the Telegram `/update` command:
  - Layer 1/2 changes → `/update layer2`
  - Layer 3 changes → `/update layer3` (choose 1 for Personal, 2 for Prop)
  - `uv sync --extra layer3` only if `pyproject.toml` changed (mention this explicitly if relevant).

### Deployment guidance for Claude

When Warren asks how to update or deploy:
- If the issue is covered by `/update`, tell him which subcommand to run.
- If not covered, debug the situation first. After resolving, ask if this should be added to `/update` for future reference.

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
| VPS #2 | Vultr | 139.180.136.233 | Windows Server | worker-personal (Fusion Markets MT5) — project folder `C:\arbitrage` |
| VPS #3 | Vultr | 45.76.156.55 | Windows Server | worker-prop (FundingPips MT5) — project folder `C:\arbitrage` |

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
| 2 — Logic Core | `layer2/logic_core.py`, `layer2/telegram_handlers.py`, `layer2/state.py` | ✅ LIVE — simultaneous MARKET execution + mismatch wording + block alerts + `_verify_and_notify` crash guard deployed 2026-05-06 |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ LIVE — journal pipeline confirmed live 2026-05-06; worker docstring VPS numbers corrected |

**Current phase**: Gate D — 7-day demo run started 2026-04-25. Target go-live: ~2026-05-03 (already past; proceed when ready).

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
| K1 — Daily loss | `(day_start − equity) / baseline × 100 ≥ max_drawdown_daily_pct` | Always −$2,000 if DD=2% and baseline=$100k. **Auto-resumes next session.** |
| K2 — Overall loss | `(baseline − equity) / baseline × 100 ≥ max_drawdown_overall_pct` | Fixed floor, e.g. $94,000 if DD=6%. Permanent halt. |
| K3 — Daily profit cap | `(equity − day_start) / baseline × 100 ≥ daily_profit_cap_pct` | Always +$2,500 if cap=2.5% and baseline=$100k. **Auto-resumes next session.** |
| K4 — Profit target | `equity ≥ baseline × (1 + profit_target_pct / 100)` | Fixed ceiling. Permanent halt. |
| K5 — Consistency | largest day / total profit < consistency_threshold_pct (Phase 2 only) | e.g. firm says 30% → stored as 29% → fires when largest day < 29%. Permanent halt. |

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
- **Execution flow — simultaneous MARKET orders for both accounts**: Layer 2 dispatches both personal and prop tickets as `order_type=market` at signal time. Layer 3 honors `ticket.get("order_type") == "market"` to bypass `LIMIT_ONLY_EXECUTION` on both workers. `_verify_and_notify()` polls both workers simultaneously (5 s initial wait, 5 s poll, 60 s max). "✅ Trade Opened" fires after both legs confirm FILLED, showing actual fill price, ticket, SL/TP, and slippage. If one or both don't fill: "⚠️ Order Not Filled — {ticker}" with per-side status.
- **XAGUSD lot sizing**: use `trade_tick_size` (0.0001), NOT `point` (0.001). Using `point` inflates lots 10×. Fixed 2026-04-22.
- **MetaTrader5 import on Linux = instant crash.** Layers 1 and 2 must never import it.
- **Price display must use `_fmt_price(symbol, price)` from `state.py`** — MT5 returns floats with binary precision artifacts (e.g. `1.3498700000000001`). `_fmt_price` rounds to the correct decimal places per instrument: JPY pairs = 3dp, XAUUSD = 2dp, XAGUSD = 4dp, all others = 5dp. Every SL/TP/entry price shown in Telegram alerts goes through this helper. Any new price display code must use it too.

- **Close detection buffer**: when one leg of a hedge closes before the other (e.g. personal SL hits one poll before prop TP), the close is held in `_pending_closes` for up to 120 s. A single combined alert fires only after both legs confirm closed or the buffer expires. This prevents duplicate split alerts and false orphan force-closes. Root cause of the session 5 split-alert incident: legs were ~2 min apart; 30 s buffer was too short.
- **Mismatch grace period**: position mismatches must persist ≥120 s (`grace = 120`) before CRITICAL MISMATCH fires. Matches the close buffer so a normal staggered close doesn't trigger a false mismatch alert.
- **Mismatch handler post-close verification**: after `_handle_mismatch()` force-closes the orphan, it waits 5 s then re-queries both accounts. If both are flat the Telegram says "✅ Resolved — both accounts are flat." If one side is still open it says "⚠️ Action required — check MT5 immediately." "Check MT5 immediately" no longer appears on a clean successful close.
- **Close alert when one side has no data**: `_send_close_alert()` shows "No matching position — already closed" (not "Still open / not confirmed") when close data is absent for one side. This is the correct wording when the position was force-closed by the mismatch handler rather than by a natural TP/SL.
- **Duplicate signal race window**: the max-positions gate counts prop positions. With simultaneous MARKET dispatch, both legs fill in < 1 s, so the window where prop count = 0 is negligible. TradingView's `in_trade` gate remains the primary guard.
- **Personal account baseline** (`pers_baseline_equity`) is set only by `/changepropfirm` wizard (Step 10/10) or `/phase2` wizard. `_update_pers_day_start()` only writes `pers_day_start_equity`; it never touches the baseline. The baseline was previously auto-set from the live MT5 balance ($10,042.75 instead of the correct $10,000) — that bug is fixed. Never auto-write `pers_baseline_equity`.
- **News stale cache fallback**: if ForexFactory calendar fetch returns empty (API down), `ff_calendar.py` returns the last good cache instead of an empty list. Prevents false "all clear" news state.
- **News suppression clear notification**: when a news suppression window expires, a grouped 🔴→🟢 Telegram alert fires (listing all pairs cleared at once) before dispatching `NEWS_CLEAR` to Layer 3. `/news` shows 🟠 per event; `/blackboard` shows 🔴 per suppressed pair.
- **`dd_floor.json` stale value on VPS #3**: Layer 3 prop worker loads `config/dd_floor.json` at startup. Layer 2 only sends `SET_PARAMETERS` (which updates this file) on explicit events (`/phase1`, `/changepropfirm` wizard). If the worker restarts with a stale/wrong floor, STATIC DD GUARD fires every 30s and blocks all trades until Layer 2 resends. Fix: run `/phase1` in Telegram (idempotent) to trigger a resend. Root cause of the 2026-04-30 incident: previous incorrect baseline entry ($1,234,567) had saved floor=$1,160,492.98. Never enter test/placeholder numbers as `baseline_equity` in the wizard.
- **Signal block alerts**: when a signal is silently dropped (system halted K1/K3, permanently halted K2/K4/K5, or news/manual suppression), a Telegram alert fires explaining the reason. Deduped via `_block_alerted` dict with 30-min cooldown per `(ticker, reason_tag)` — prevents spam when TradingView sends repeated signals while blocked. Three paths: ⏸ halted, 🔴 permanently halted, 📰 suppressed.
- **`_verify_and_notify` crash guard**: the order-confirmation task body lives in `_verify_and_notify_inner()`. The outer `_verify_and_notify()` wraps it in try/except — any crash sends a Telegram alert ("⚠️ Internal Error — check VPS #1 logs") instead of silently disappearing via `asyncio.create_task()` exception swallowing.
- **Windows VPS project folder**: both VPS #2 and VPS #3 use `C:\arbitrage` (NOT `C:\arbitrage-trading`). Workers launched via `uv run python layer3/worker_personal.py` from that directory. `load_dotenv()` in `_worker_core.py` loads `C:\arbitrage\.env` from CWD. Firebase service account path: `C:\arbitrage\secrets\firebase-service-account.json`. The `secrets\` folder is gitignored and must be created manually on VPS #2 only.

---

## Hard Constraints

- Personal account always trades **opposite** direction to prop firm.
- Lot sizing uses `baseline_equity × 0.67%` — never live equity.
- Personal lots are sized independently so that **if the personal SL hits, the loss equals exactly `prop_dollar_risk × phase_ratio`** (e.g. $670 × 0.20 = $134 in Phase 1). Formula: `pers_lots = pers_dollar_risk / (sl_distance × contract_size)` for xxxUSD pairs. The old formula `prop_lots × phase_ratio` kept the lot ratio but caused dollar risk at the personal SL to scale with sl_distance — this caused unexpected large losses when personal SL hit. Do NOT revert to `prop_lots × phase_ratio`.
- Prop firm config: wizard-only (`/changepropfirm`). Never edit `propfirm_config.json` manually.
- **`baseline_equity` is immutable** — only written by explicit user commands: `/changepropfirm` wizard (Step 9/10), `/phase1` (only when baseline is 0), `/phase2` wizard. `_update_day_start()` NEVER touches it — only `day_start_equity` and `day_start_date_utc`. Nothing automatic can overwrite it. `/setbaseline` command does NOT exist — was removed; re-run wizard Step 9/10 to correct prop baseline.
- **`pers_baseline_equity` is manual-only** — only written by `/changepropfirm` wizard (Step 10/10) or `/phase2` wizard. `_update_pers_day_start()` only writes `pers_day_start_equity`. Never auto-set from live MT5 balance.
- Phase switching: Telegram-only (`/phase1`, `/phase2`).
- ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open between VPS #1 and VPS #2/#3.
- TradingView Premium required for webhook delivery.
- One TradingView chart per instrument — 8 charts, 8 pairs (NAS100 removed).
- Demo-first mandatory: ≥7 trading days before live capital.

---

## Current State (as of 2026-05-07, session 10)

All four layers deployed and operational. Gate D demo run in progress (7-day window passed; proceed to live when ready).

- Layer 0: 8 alerts active. `in_trade` gate live. **Signal engine is frozen — do not touch.**
- Layer 1: Live. News filter active. Stale-cache fallback on FF calendar failure deployed.
- Layer 2: Full feature set deployed:
  - Kill 1/2/3/4/5 use static `baseline_equity` divisor — all thresholds are fixed dollar amounts
  - K1 layered floors from baseline (staircase). K2 hard floor safety net. K3 daily cap from day_start. K4 cumulative profit target. K5 consistency (Phase 2 only).
  - **K1 and K3 are daily halts — auto-resume at next session open (12:00 SGT) via `daily_halted` + `daily_halted_date` flags.** K2/K4/K5 are permanent halts requiring manual action. `/resume` still works as a manual override for K1/K3 and also clears the daily halt flags.
  - `/phase1` is idempotent — will not overwrite an existing baseline mid-evaluation
  - 120 s close-detection buffer and 120 s mismatch grace — prevents split alerts and false CRITICAL MISMATCH when legs close ~2 min apart
  - All Telegram alerts use "Personal Signal" and "Prop Hedge" labels — no VPS numbers in user-facing output. Personal Signal always listed first.
  - **Simultaneous MARKET execution (reverted 2026-05-06)**: both personal and prop tickets dispatched as `order_type=market` at signal time. `_verify_and_notify()` polls both simultaneously (60 s max). Telegram flow: ✅ Trade Opened (with actual MT5 fill price, ticket, SL/TP, slippage). If one or both don't fill: "⚠️ Order Not Filled — {ticker}" with per-side status.
  - **Mismatch alert (updated 2026-05-06)**: `_handle_mismatch()` re-queries both accounts 5 s after force-close. Message says "✅ Resolved — both accounts are flat." or "⚠️ Action required" based on actual verified state. Position closed alert shows "No matching position — already closed" instead of "Still open / not confirmed" when one side has no data.
  - Position Closed alert: title = 🟢 Take Profit / 🔴 Stop Loss based on Personal P&L; sections: Personal Signal, Prop Hedge, After Close, Equity
  - `/equity`: Baseline, Balance, Equity, Floating P&L, Overall P&L per account; Personal Signal first, Prop Hedge second.
  - `/checkaccount`: queries both Layer 3 workers via ZMQ REQ and shows MT5 login + server for each account (no password transmitted)
  - `/update`: `local` / `layer2` / `layer3` (1=Personal, 2=Prop) / `account`
  - Dynamic trading window: `config/trading_window.json` + `/setwindow`. Currently 12:00–00:00 SGT.
  - `trade_allowed` monitoring. News suppression clear notification (grouped 🔴→🟢).
  - `/news` shows 🟠 per event; `/blackboard` shows 🔴 per pair, 🟢 when clear
  - `/changepropfirm` wizard (10 steps, `/back` supported). Buffer rules: Overall DD no buffer, Daily DD −1pp, Consistency −1pp, daily cap = 25% of target (auto).
  - All SL/TP/entry prices use `_fmt_price(symbol, price)` — no float artifacts.
- Layer 3: Both workers running (VPS #2 personal 106497299, VPS #3 prop 106496748, both MetaQuotes demo). Layer 3 is only dormant on weekends; trading hours controlled entirely by Layer 2 `/setwindow`. Both workers honor `order_type=market` ticket field — Layer 2 always sends `order_type=market` for all tickets. `LIMIT_ONLY_EXECUTION` env var is overridden by the market ticket field.
- **Trade Journal (session 9 — fully operational, VPS #2 only)**:
  - `layer3/journal/` package: `firebase_journal.py`, `rr_chart_renderer.py`, `storage_uploader.py`, `screenshot_capture.py`, `journaling_worker.py`, `retry_queue.py`, `pending_deals_queue.py`
  - `_position_close_watcher()` daemon thread polls MT5 every 5 s, detects TP/SL closes by magic number, fires journal pipeline
  - On close: fetches deal history (with 7-retry backoff [5,10,20,40,60,120,180]s — ~7 min total) → renders dark-theme outcome chart (matplotlib Agg) → uploads PNG to Firebase Storage → writes Firestore doc with `rrChartUrl` + `outcomeScreenshotUrl`
  - Document ID: `{accountType}_{mt5AccountId}_{ticket}` (deterministic, upsert-safe)
  - Retry queue (`journal_retry_queue.jsonl`) for failed Firestore writes, retried every 300 s
  - **Persistent deal retry queue** (`journal_pending_deals.jsonl`, gitignored): if 7-retry inline loop fails (MetaQuotes Demo history takes >7 min to sync), position is enqueued here. Background thread (`pending_deals_queue.py`) retries every 10 min for up to 24h, then drops with a warning. Telegram notifications sent at: enqueue ("📋 Journal Queued"), every 3h still pending ("⏳ Journal Still Pending"), success ("✅ Journal Recovered"), 24h drop ("⚠️ Journal Failed"). **VPS #2 `.env` must have `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for these alerts to fire** — same values as VPS #1.
  - **⚠️ KNOWN ISSUE (session 11 must fix)**: Journal pipeline has additional unresolved bugs — Warren to report specific error logs next session. `from_dt` timezone shows `+07:00` instead of `+00:00` in logs (possible MT5 position `time` field timezone mismatch on VPS #2). Prioritise debugging this after the next trade closes. The persistent queue is a workaround, not the root-cause fix.
  - **VPS #2 (personal): `FIREBASE_JOURNAL_ENABLED=true`, `FIREBASE_JOURNAL_DRY_RUN=false`. `SCREENSHOT_STORAGE=firebase`, `SCREENSHOT_DRY_RUN=false`, `FIREBASE_STORAGE_BUCKET=gen-lang-client-0206326169.firebasestorage.app` — Firebase Storage LIVE (upgraded to Blaze plan 2026-05-07). Screenshots upload to `trade-screenshots/{accountType}/{mt5AccountId}/{ticket}/outcome.png`. `FIREBASE_SERVICE_ACCOUNT_PATH=C:\arbitrage\secrets\firebase-service-account.json`.**
  - **VPS #3 (prop): `FIREBASE_JOURNAL_ENABLED=false` — journal disabled, prop trades not recorded**
  - Website: warrenlimzf.com/journal reads from Firestore collection `users/{userId}/trades`
  - Firebase project: `gen-lang-client-0206326169`. Plan: **Blaze (pay-as-you-go)**. User ID (wanttobefire@gmail.com — dedicated journal account): `WCzOHPl8C4Q1aa3EDHkOGhdH9To1`. Database ID: `ai-studio-88ba4d0a-7b6e-4d07-a03b-675ed3bc8607` (named, not default — must set FIREBASE_DATABASE_ID in .env). Storage bucket: `gen-lang-client-0206326169.firebasestorage.app`.
  - Connectivity test: `python scripts/test_firebase_write.py` (writes real Firestore doc, reads back)
  - Manual backfill: `uv run python scripts/backfill_journal.py` — one-off script for missed trades; hardcoded per ticket, edit as needed
  - **Storage cache**: `storage_uploader.py` sets `cache_control="no-cache, no-store, must-revalidate"` and appends `?t={timestamp}` to the public URL so the journal website always fetches fresh images
  - **Chart renderer fixes (session 10)**: `rr_chart_renderer.py` — fixed `open_idx`/`close_idx` using `dt.tz_localize(None).values` so searchsorted gives correct bar index (tz-aware `.values` silently returned wrong index ~10 bars early); added M15 to bottom-left meta label. Screenshot filenames are `{accountType}_{ticket}_outcome.png` (not generic `outcome.png`).
  - **Key bugs fixed (sessions 9–10)**: (1) MT5 deal history lag — history unavailable at close time; extended retries to 7 steps (~7 min). (2) Firestore 404 — `firebase_admin` connected to `(default)` DB; fixed by using `google.cloud.firestore.Client` with named `database=FIREBASE_DATABASE_ID`. (3) Storage on Spark plan — upgraded to Blaze; `storage_uploader.py` rewritten to use `google.cloud.storage.Client`. (4) Entry bar wrong — tz-aware `.values` + naive `datetime64` searchsorted mismatch; fixed with `dt.tz_localize(None).values`.

- **Lot sizing — CORRECT procedure (do not change)**:
  1. `prop_dollar_risk = baseline_equity × 0.67%` (e.g. $100k × 0.0067 = $670)
  2. Prop lots: `prop_lots = prop_dollar_risk / (tp_distance × contract_size)` for xxxUSD — sized so prop risks $670 if prop SL (= signal TP) hits
  3. Personal lots: `pers_lots = prop_lots × phase_ratio` (Phase 1 = 0.20, Phase 2 = 0.70) — fixed ratio of prop lots
  4. Personal dollar risk: `pers_dollar_risk = pers_lots × sl_distance × contract_size` — derived result, varies per trade (e.g. ~$134 in Phase 1 but depends on SL distance). This is intentional — personal risk scales with the trade's SL distance.

**Next action**: Wait for next TP/SL close on VPS #2 — confirm persistent journal queue works (📋 Queued Telegram fires, then ✅ Recovered or ⚠️ Failed). Also add `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` to VPS #2 `.env`. Then: switch to real Fusion Markets + FundingPips accounts when ready.
