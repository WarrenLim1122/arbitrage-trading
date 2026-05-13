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

## Layer 0 — Signal Engine (`layer0/1D-15m Breakout INDICATOR.pine`, Pine Script v6)

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

`layer0/1D-15m Breakout STRATEGY.pine` — same logic with `strategy()` for Strategy Tester.

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
| `/changepropfirm` | 10-step wizard — collects raw limits, applies buffers, saves config. No firm name step. Step 9 = prop baseline (`baseline_equity`), Step 10 = personal baseline (`pers_baseline_equity`). |
| `/consistency` | Phase 2 daily profit breakdown and consistency rule status |
| `/propfirm` | Display current prop firm config |
| `/equity` | Baseline, Balance, Equity, Floating P&L, and Overall P&L per account (Personal Signal first, Prop Hedge second) |
| `/checkaccount` | Query Layer 3 workers via ZMQ REQ — shows MT5 login + server for each account (no password transmitted) |
| `/update` | Deployment guide: `local` (push to GitHub), `layer2` (deploy VPS #1), `layer3` (update a Layer 3 worker — prompts 1=Personal or 2=Prop), `account` (MT5 change checklist) |
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
- **"✅ Trade Opened — TICKER"** with per-account status (Personal Signal, Prop Hedge).
- **"⚠️ Execution Issue — TICKER"** with per-account error and "Check MT5 on both accounts immediately."

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

**CRITICAL: K1 daily drawdown is DYNAMIC** — calculated from `day_start_equity` (the account balance at session open), NOT from `baseline_equity`. The daily dollar loss limit changes each session as the account grows or shrinks. Example: account at $103k, daily DD = 2% → max daily loss = $103k × 2% = $2,060 → floor = $100,940 today.

**K2/K3/K4 are STATIC** — all calculated from `baseline_equity`. Fixed dollar amounts for the entire evaluation regardless of how the account moves.

| Kill | Phase | Trigger condition | Formula / Example | Action |
|---|---|---|---|---|
| K1 — Daily loss | All | `equity ≤ day_start − (day_start × max_drawdown_daily_pct / 100)` | **DYNAMIC**: floor = $103k − ($103k × 2%) = $100,940. Resets each session. | FORCE_CLOSE + halt — **auto-resumes next session** |
| K2 — Overall loss | All | `equity ≤ baseline × (1 − max_drawdown_overall_pct / 100)` | **STATIC**: e.g. $100k × (1 − 6%) = $94,000 fixed floor. | FORCE_CLOSE + permanent halt |
| K3 — Daily profit cap | All | `equity ≥ day_start + (baseline × daily_profit_cap_pct / 100)` | Cap amount static from baseline (+$2,500 if cap=2.5% and baseline=$100k), but cap level shifts with day_start. | FORCE_CLOSE + halt — **auto-resumes next session** |
| K4 — Profit target | All | `equity ≥ baseline × (1 + profit_target_pct / 100)` | **STATIC**: fixed ceiling. | FORCE_CLOSE + permanent halt → `/phase2` |
| K5 — Consistency | Phase 2 | `largest day / total profit < consistency_threshold_pct` AND ≥2 profitable days | e.g. firm says 30% → stored as 29% → fires when largest day < 29%. | FORCE_CLOSE + permanent halt → payout claim |

**Buffers applied automatically:**

- `daily_profit_cap_pct` is auto-set to `profit_target_pct × 0.25` (25% of target — enforces before the 30% consistency threshold).
- `max_drawdown_daily_pct` enforced after −1pp buffer (firm says 3% → bot triggers at 2%).
- `consistency_threshold_pct` also buffered −1pp automatically (firm says 30% → stored/enforced at 29%).
- `/phase1` is idempotent — re-running it mid-evaluation does NOT overwrite an existing baseline.
- `/resume` clears daily halt flags (K1/K3) manually before auto-resume.

**`trade_allowed` monitoring (deployed 2026-04-27):** equity monitor reads `trade_allowed` from both workers every 30s. Immediate Telegram alert when MT5 disables algo trading, cleared when restored. Fires once per state change.

**Worker health monitoring:** 3 consecutive timeouts (~90s) → Telegram alert to restart. Recovery also alerted.

**Position mismatch monitoring (every 30s):**

| Type | Condition | Action |
|---|---|---|
| prop_only | Ticker on prop, missing on personal ≥120s | Close orphan on prop |
| pers_only | Ticker on personal, missing on prop ≥120s | Close orphan on personal |
| same_direction | Both accounts same direction | Close on BOTH + alert |

### SGT Curfew Gate / Trading Window

- Stored in `config/trading_window.json` — `current_window` (start/end HH:MM SGT) and `next_window` (optional, applied at 11:00 SGT session rollover).
- **Default: 12:00–00:00 SGT, weekdays only.** `00:00` end = midnight (treated as 1440 minutes internally).
- Change via `/setwindow HH:MM HH:MM` Telegram command — choose "today" (immediate) or "tomorrow" (next rollover).
- `_is_sgt_curfew()` reads from `_trading_window` dict dynamically — no restart needed after `/setwindow`.
- Signals outside the window or on weekends: rejected immediately, no state change.
- At window close: monitor thread dispatches FORCE_CLOSE (`halt=False`) — positions closed, `active` untouched. Resumes automatically at next window open on a weekday.
- Weekends always curfew regardless of window setting.
- **`00:00` is ambiguous — handled by `is_end` flag in `_window_minutes(t_str, is_end=False)`**: as a start time `00:00` = 0 min; as an end time `00:00` = 1440 min (midnight). Without this, a 24-hour window (`00:00–00:00`) would cause permanent curfew because both start and end would resolve to 1440. Always pass `is_end=True` when calling `_window_minutes` for the end time.
- **Layer 3 has NO time-of-day curfew of its own.** The `/setwindow` window in Layer 2 is the sole gate for execution hours. Layer 3's `_sgt_scheduler` only sets `_dormant = True` on weekends (`weekday >= 5`). Any time-of-day logic in `_sgt_scheduler` must not be re-added — it caused EXECUTION FAILURE spam (Layer 2 dispatched, Layer 3 silently dropped, 5s check found no positions). Fixed 2026-05-01.
- **`/status` Active vs Curfew are independent**: "Status: 🟢 Active" means the engine is armed (not halted). "Curfew: Yes — dormant" means current time is outside the window. Both can be true simultaneously — engine ready but window closed, no trades until window opens.

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

**REP socket reply includes `trade_allowed`, `account_login`, `account_server`, `account_name`** — Layer 2 uses these for algo-trading monitoring and `/checkaccount`.

### Layer 3 SGT Schedule

- Dormant: weekends only (Saturday/Sunday all day). No weekday time-of-day restriction — Layer 2 `/setwindow` is the sole gate for execution hours. Hard-coded `h < 12` weekday curfew was removed 2026-05-01.
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

## MT5 / Layer 3 Operational Gotchas

Read this section before touching Layer 3, MT5, or order execution code.

- **"Disable algorithmic trading when the account has been changed"** (MT5 → Tools → Options → Expert Advisors) must be **unchecked** on both VPS #2 and VPS #3. If checked, MT5 silently disables algo trading after any account change — orders are rejected with no error in Layer 3. Root cause of the 2026-04-24 NZDUSD silent failure. Uncheck once; it persists.
- **`trade_allowed` monitoring**: equity monitor reads this flag from both workers every 30s via ZMQ REP. Immediate Telegram alert if MT5 auto-disables algo trading, with step-by-step fix instructions.
- **Execution flow — simultaneous MARKET orders for both accounts**: Layer 2 dispatches both personal and prop tickets as `order_type=market` at signal time. Layer 3 honors `ticket.get("order_type") == "market"` to bypass `LIMIT_ONLY_EXECUTION` on both workers. `_verify_and_notify()` polls both workers simultaneously (5 s initial wait, 5 s poll, 60 s max). "✅ Trade Opened" fires after both legs confirm FILLED, showing actual fill price, ticket, SL/TP, and slippage. If one or both don't fill: "⚠️ Order Not Filled — {ticker}" with per-side status.
- **XAGUSD lot sizing**: use `trade_tick_size` (0.0001), NOT `point` (0.001). Using `point` inflates lots 10×. Fixed 2026-04-22.
- **MetaTrader5 import on Linux = instant crash.** Layers 1 and 2 must never import it.
- **Price display must use `_fmt_price(symbol, price)` from `state.py`** — MT5 returns floats with binary precision artifacts (e.g. `1.3498700000000001`). `_fmt_price` rounds to correct decimal places per instrument: JPY pairs = 3dp, XAUUSD = 2dp, XAGUSD = 4dp, all others = 5dp. Every SL/TP/entry price shown in Telegram alerts goes through this helper. Any new price display code must use it too.
- **Close detection buffer**: when one leg of a hedge closes before the other (e.g. personal SL hits one poll before prop TP), the close is held in `_pending_closes` for up to 120 s. A single combined alert fires only after both legs confirm closed or the buffer expires. Prevents duplicate split alerts and false orphan force-closes. Session 5 split-alert incident had legs ~2 min apart; 30 s buffer was too short.
- **Mismatch grace period**: position mismatches must persist ≥120 s (`grace = 120`) before CRITICAL MISMATCH fires. Matches the close buffer so a normal staggered close doesn't trigger a false mismatch alert.
- **Mismatch handler post-close verification**: after `_handle_mismatch()` force-closes the orphan, it waits 5 s then re-queries both accounts. If both are flat the Telegram says "✅ Resolved — both accounts are flat." If one side is still open it says "⚠️ Action required — check MT5 immediately." "Check MT5 immediately" no longer appears on a clean successful close.
- **Close alert when one side has no data**: `_send_close_alert()` shows "No matching position — already closed" (not "Still open / not confirmed") when close data is absent for one side. Correct wording when the position was force-closed by the mismatch handler rather than by a natural TP/SL.
- **Duplicate signal race window**: the max-positions gate counts prop positions. With simultaneous MARKET dispatch, both legs fill in < 1 s, so the window where prop count = 0 is negligible. TradingView's `in_trade` gate remains the primary guard.
- **Personal account baseline** (`pers_baseline_equity`) is set only by `/changepropfirm` wizard (Step 10/10) or `/phase2` wizard. `_update_pers_day_start()` only writes `pers_day_start_equity`; it never touches the baseline. The baseline was previously auto-set from the live MT5 balance ($10,042.75 instead of the correct $10,000) — that bug is fixed. Never auto-write `pers_baseline_equity`.
- **News stale cache fallback**: if ForexFactory calendar fetch returns empty (API down), `ff_calendar.py` returns the last good cache instead of an empty list. Prevents false "all clear" news state.
- **News suppression clear notification**: when a news suppression window expires, a grouped 🔴→🟢 Telegram alert fires (listing all pairs cleared at once) before dispatching `NEWS_CLEAR` to Layer 3. `/news` shows 🟠 per event; `/blackboard` shows 🔴 per suppressed pair.
- **`dd_floor.json` stale value on VPS #3**: Layer 3 prop worker loads `config/dd_floor.json` at startup. Layer 2 only sends `SET_PARAMETERS` (which updates this file) on explicit events (`/phase1`, `/changepropfirm` wizard). If the worker restarts with a stale/wrong floor, STATIC DD GUARD fires every 30s and blocks all trades until Layer 2 resends. Fix: run `/phase1` in Telegram (idempotent) to trigger a resend. Root cause of the 2026-04-30 incident: previous incorrect baseline entry ($1,234,567) had saved floor=$1,160,492.98. Never enter test/placeholder numbers as `baseline_equity` in the wizard.
- **Signal block alerts**: when a signal is silently dropped (system halted K1/K3, permanently halted K2/K4/K5, or news/manual suppression), a Telegram alert fires explaining the reason. Deduped via `_block_alerted` dict with 30-min cooldown per `(ticker, reason_tag)` — prevents spam when TradingView sends repeated signals while blocked. Three paths: ⏸ halted, 🔴 permanently halted, 📰 suppressed.
- **`_verify_and_notify` crash guard**: the order-confirmation task body lives in `_verify_and_notify_inner()`. The outer `_verify_and_notify()` wraps it in try/except — any crash sends a Telegram alert ("⚠️ Internal Error — check VPS #1 logs") instead of silently disappearing via `asyncio.create_task()` exception swallowing.
- **Windows VPS project folder**: both VPS #2 and VPS #3 use `C:\arbitrage` (NOT `C:\arbitrage-trading`). Workers launched via `uv run python layer3/worker_personal.py` from that directory. `load_dotenv()` in `_worker_core.py` loads `C:\arbitrage\.env` from CWD. Firebase service account path: `C:\arbitrage\secrets\firebase-service-account.json`. The `secrets\` folder is gitignored and must be created manually on VPS #2 only.

---

## Telegram Alert Formats (session 12)

### Trade Opened

- Title: symbol first (`XAUUSD — Trade Opened`), no ✅.
- Direction merged into section headers: `Personal Signal — ↑ LONG`, `Prop Hedge — ↓ SHORT`. Personal Signal always listed first.
- Each field on its own line: Size / Entry / SL / TP / Risk / Reward / RR / Ticket.
- RR format: `0.27` (no `1:` prefix).
- Footer: `Phase: Phase 1` / `Baseline: $100,000` (no decimals).
- Entry slippage `(req ..., diff ...)` removed from output.
- "Order Not Filled" alert: `⚠️ Order Not Filled — {ticker}` with per-side status.

### Trade Closed

- Title: `{emoji} {symbol} — {Take Profit | Stop Loss | News Close | Position Closed}`.
- Emoji selection driven by Layer 3 deal reason when available (🟢 TP / 🔴 SL / ⚠️ BOT_LOGIC|MANUAL), or by personal P&L sign as fallback on demo.
- 📰 News Close fires when Layer 2's `_news_close_dispatched` dict (10-min TTL, populated when Layer 2 dispatches `pre_news_*` close) has an entry for that symbol.
- Each side block: `Personal Signal — ↑ LONG` / `Prop Hedge — ↓ SHORT` header with one variable per line: Size / Entry / Exit / Reason / P&L / Commission / Ticket.
- Exit price from `deal.price` (actual fill), not theoretical SL/TP level.
- P&L is net (`gross + commission + swap`); commission shown on a separate line for transparency.
- Footer logic (auto):
  - Demo + deal data missing → `ℹ️ Demo account — exact MT5 figures will sync to the journal in ~2-3h.`
  - Real + deal data missing → `⚠️ Deal data unavailable from broker — check journal dashboard shortly.`
  - Any account with both sides' deal data present → no footer.

### Mismatch / News

- Mismatch alert (`_handle_mismatch()`): re-queries both accounts 5 s after force-close. Says "✅ Resolved — both accounts are flat." or "⚠️ Action required".
- Position closed alert: "No matching position — already closed" when one side has no data.
- News Pre-Close: ONE grouped message per currency event (not one per pair). Shows only the positions being closed by that specific event, split into Personal Signal / Prop Hedge sections. The TP/SL close alert then fires per pair as normal. `_TICKER_CURRENCIES` in `config/allowed_pairs.json` controls which pairs are affected by which currency — XAUUSD/XAGUSD = `["USD"]` so they close on USD news.

---

## Trade Journal Architecture (session 12 — immediate screenshot)

VPS #2 only. The journal pipeline records every closed trade to Firebase Firestore + Storage for `warrenlimzf.com/journal`.

**Package**: `layer3/journal/` — `firebase_journal.py`, `rr_chart_renderer.py`, `storage_uploader.py`, `screenshot_capture.py`, `journaling_worker.py`, `retry_queue.py`, `pending_deals_queue.py`.

**Detection**: `_position_close_watcher()` daemon thread polls MT5 every 5 s, detects closes by magic number, fires the journal pipeline for **ALL** close types (TP, SL, news, manual).

**Two-phase pipeline (screenshot decoupled from deal history):**

- **Phase 1 (immediate)**: `_position_close_watcher` stamps `close_time_detected = now()` and `close_price_est = last tick bid/ask` into the snapshot the instant the position disappears. Journal thread calls `_take_screenshot_immediate()` using snapshot + candle data (always available) — renders and uploads PNG before waiting for deal history. Screenshot URL stored in snapshot as `_screenshot_fields`.
- **Phase 2 (whenever deal history arrives)**: `history_deals_get()` fetched with 7-retry backoff. On success: Firestore write using Phase 1 screenshot URL + actual P&L/commission/swap. If all retries fail: snapshot (including `_screenshot_fields`) queued — pending queue carries screenshot forward, so the Firestore write is the only thing delayed.

**Why this matters**: MetaQuotes Demo deal history syncs asynchronously (2-3h delay). Previously, screenshot was chained AFTER deal history — queued trades got no screenshot. Candle data has no delay (market data, not account-specific). On real Fusion Markets, deal history arrives in < 1 s so Phase 1 and Phase 2 complete back-to-back.

**Snapshot isolation**: each closed ticket gets its own `pop()`-ed snapshot dict. Multiple simultaneous news closes (e.g. 5 pairs at once) each have an independent snapshot — `close_time_detected` and `close_price_est` cannot overwrite each other.

**Operational details:**

- `SCREENSHOT_ONLY_FOR_TP_SL` defaults to `false` (session 12 fix) — screenshots taken for ALL close types (TP, SL, NEWS, BOT_LOGIC, MANUAL). Chart badge shows `WIN  RR 0.27` (no $ amount) when `net_pnl=None` at Phase 1 time.
- Document ID: `{accountType}_{mt5AccountId}_{ticket}` (deterministic, upsert-safe).
- Retry queue (`journal_retry_queue.jsonl`) for failed Firestore writes — retried every 300 s.
- **Persistent deal retry queue** (`journal_pending_deals.jsonl`, gitignored): if 7-retry inline loop fails, position is enqueued. Background thread retries every **2 hours** for up to 24 h. Telegram notifications: enqueue ("📋 Journal Queued"), every 3 h still pending ("⏳ Journal Still Pending"), success ("✅ Journal Recovered"), 24 h drop ("⚠️ Journal Failed"). **VPS #2 `.env` must have `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.**
- **`from_dt` fix (session 11)**: MT5 MetaQuotes Demo server returns `position.time` offset by ~3 h (server UTC+3 timezone). `from_dt = min(open_time − 2h, now − 6h)` prevents inverted query range. MetaQuotes Demo deal history can take 2-3h — expected server latency, not a bug. On real brokers: instant.
- **News close tagging (session 11)**: before closing positions via `CLOSE_TICKER`, `_force_close_ticker()` tags each position in `_known_positions` with `close_reason_override = "NEWS"` when `reason.startswith("pre_news")`. The journal pipeline reads this override so news-triggered closes are identified correctly.
- **Demo/live auto-detection**: Layer 3 caches `_account_mode` (`demo`/`real`/`contest`) at MT5 connect by reading `account_info().trade_mode`. Embedded in every `deal_pnl` ZMQ reply so Layer 2 always knows what kind of account it's dealing with. No env var. Switching to live Fusion Markets just requires a worker restart — the `(est.)` labels and demo footer auto-disappear.

**Environment per VPS:**

| VPS | Env config |
|---|---|
| VPS #2 (personal) | `FIREBASE_JOURNAL_ENABLED=true`, `FIREBASE_JOURNAL_DRY_RUN=false`, `SCREENSHOT_STORAGE=firebase`, `SCREENSHOT_DRY_RUN=false`, `FIREBASE_STORAGE_BUCKET=gen-lang-client-0206326169.firebasestorage.app`, `FIREBASE_SERVICE_ACCOUNT_PATH=C:\arbitrage\secrets\firebase-service-account.json` |
| VPS #3 (prop) | `FIREBASE_JOURNAL_ENABLED=false` — journal disabled, prop trades not recorded |

**Firebase project**: `gen-lang-client-0206326169` (Blaze plan). User ID (`wanttobefire@gmail.com`): `WCzOHPl8C4Q1aa3EDHkOGhdH9To1`. Database ID: `ai-studio-88ba4d0a-7b6e-4d07-a03b-675ed3bc8607` (named — must set `FIREBASE_DATABASE_ID` in `.env`). Storage bucket: `gen-lang-client-0206326169.firebasestorage.app`. Website reads from Firestore collection `users/{userId}/trades`.

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
  "propfirm_name":             "Prop Account",
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
`baseline_equity` = prop firm initial account balance entered in wizard Step 9/10. **Never fetched from live MT5.** Immutable for the life of the evaluation — only `/changepropfirm` wizard or `/phase1` (when 0) can change it. `/setbaseline` command does not exist.
`pers_baseline_equity` = personal account balance entered in wizard Step 10/10. Set only by `/changepropfirm` or `/phase2` wizard — never auto-set.
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
  [ ] "✅ Trade Opened" Telegram fires on every dispatch
  [ ] "⚠️ Execution Issue" fires and shows correct error when a worker is down
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
3. Send `/changepropfirm` — re-run wizard with real FundingPips limits. At Step 9/10 enter the prop firm's initial account balance (e.g. `100000`) — this becomes `baseline_equity`. At Step 10/10 enter the personal account starting balance — this becomes `pers_baseline_equity`.
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
