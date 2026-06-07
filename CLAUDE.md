# CLAUDE.md

Operational guide for Claude Code. For everything reference-shaped — risk math, kill condition formulas, layer deep-dives, MT5 gotchas, Telegram message formats, deployment gates, go-live checklist — read **`TECHNICAL.md`**.

---

## Workflow Rules

- **Auto-push to GitHub after every code change.** Warren has given standing permission for all pushes to `main`. Never wait to be reminded — commit and push immediately after making any file edits.
- **After a push, tell Warren which Telegram `/update` commands to run** — do not repeat full deployment steps in responses. Routine deployment instructions now live inside the Telegram `/update` command:
  - Layer 1/2 changes → `/update layer2`
  - Layer 3 changes → `/update layer3` (choose 1 for Personal, 2 for Prop)
  - `uv sync --extra layer3` only if `pyproject.toml` changed (mention this explicitly if relevant).
- When Warren asks how to update or deploy:
  - If the issue is covered by `/update`, tell him which subcommand to run.
  - If not covered, debug first. After resolving, ask if it should be added to `/update`.

---

## 🧠 Knowledge base — CONSULT FIRST

**The KB is built: `docs/reference/` (start at `index.md`).** It is the authoritative, code-verified
reference layer — architecture, calculations (risk/lot/Phase 1+2 geometry/kills), Telegram messages,
Layer 3 execution, deployment. **Before editing code for any request, read the relevant KB page to
locate the exact file:line, then act.** **Keep the KB in sync in the same session as any code change.**
Memory: [[knowledge-base-workflow]].

## 🔔 Next Session — RESUME FROM `docs/SESSION_HANDOFF.md`

Surface this the first time Warren returns:

**Read `docs/SESSION_HANDOFF.md`** — it carries the in-flight delta.

**Still-pending deploy (carry-over, sessions 15–18):** `/update layer2` (Telegram changes — incl. session-18 `/phase1` fee-anchor reset) AND `/update layer3` ×2 (`_worker_core.py` + `journaling_worker.py` changed across sessions 16–17). No `pyproject.toml` change → no `uv sync`. **CRITICAL: the personal worker (VPS #2) is still on pre-session-17 code** — that's why personal `/equity` shows `Trading Fee: SGD −12.40` (full since-open residual, no anchor) while prop shows `$0`. Ctrl+C and re-run `worker_personal.py` after `git pull` (git pull alone does NOT reload). After workers restart: `/checksymbols`; close one trade (alert ≤30s, real P&L, no `(est.)`); run `/phase1`/`/phase2`/`/changepropfirm` once so the per-cycle fee anchor is captured on BOTH workers. To start Phase 1 on the live $50k account: `/phase1` → `4500:1000` → `CONFIRM`. See `## Current State` below.

Lower-priority queued (not yet done):
1. **Folder reorganization** — DONE (session 18). The accd561 deletion table is fully cleared: superpowers/, AI_Workflow.md, backfill_journal.py, TEST-ONLY pine, skill-creator were already gone; scripts reorganized into `dev-tests/` + `vps-setup/`; empty `*.log` removed; `docs/README.md` de-linked from the dead AI_Workflow.md and pointed at the new KB. Only residue: a root `.DS_Store` (gitignored, env-locked).
2. **Message-structure spec** (optional, Warren deferred) — the ━ header + `Label: value` format is now the de-facto standard across ALL alerts AND command outputs and is documented in `docs/reference/messages.md`; a one-paragraph written spec in TECHNICAL.md would formalize it but isn't blocking.

**Already shipped (don't re-do):**
- All Telegram message text lives in `layer2/telegram_handlers.py` as named `msg_*()` functions; `logic_core.py` is pure orchestration. `/messages` + `/messages2` print the catalog.
- **All alert templates AND all on-demand command outputs** use the `━`×12 header + `Label: value` format. Commands route through `_cmd_header()` + `_cmd_pos_block()`; alerts through `_MSG_SEP`. (Message-formatting work from sessions 12-14 is complete — the old "apply 20-37 fixes to 1-19" task is obsolete; the full restructure superseded it.)
- Money is currency-correct everywhere: prop `$` (USD), personal in MT5-reported account currency (SGD) via `_msg_signed_money(value, currency)`; forex prices carry no symbol.

---

## Project

Automated Trade Execution Engine — 4-layer cross-hedging system. Personal account (Fusion Markets) follows signal direction; prop firm account (FundingPips) executes the **inverse** as a hedge. Sizing is phase-dependent, controlled via Telegram.

## Architecture

```
TradingView (15m chart — one chart per pair)
  └── layer0/1D-15m Breakout INDICATOR.pine
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

VPS #1 layers run as systemd services (auto-restart). VPS #2/#3 workers run in PowerShell — must be manually restarted after VPS reboot. Do NOT close the PowerShell window; closing the noVNC browser tab is safe.

## Build Status

| Layer | Files | Status |
|---|---|---|
| 0 — Signal Engine | `layer0/1D-15m Breakout INDICATOR.pine` | ✅ LIVE — 7 alerts active (XAGUSD + NAS100 dropped 2026-05-29), `in_trade` gate deployed 2026-04-27. **Frozen — do not edit without asking Warren first.** |
| 1 — Gatekeeper | `layer1/main.py`, `news_filter.py`, `ff_calendar.py` | ✅ LIVE — systemd on VPS #1 |
| 2 — Logic Core | `layer2/logic_core.py`, `telegram_handlers.py`, `state.py` | ✅ LIVE — Phase 1/Phase 2 are DIFFERENT geometries (Phase 1 = fixed-lot/moving-TP, rewritten session 22; Phase 2 = full-signal box). See `docs/reference/calculations.md`. Pending `/update layer2`. |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ **Live cutover UNBLOCKED (2026-05-26)** — both VPS desktops streaming live (personal 448196 SGD + prop 20047930 USD). Connection rewrite shipped (`72b3921` + `75f55f5`): self-launch + hard account guard. Awaiting `git pull` + worker start on both VPSes. See Current State + VPS MT5 Setup. |

## Covered Instruments — single source of truth: `config/symbols.json`

The canonical registry (`config/symbols.json`, loaded by `layer2/symbols.py`) is **the** list. **33 symbols** today (31 FX + XAUUSD + XAGUSD): 7 majors, 8 Asian, 4 other, 12 exotic/NDF, 2 metals. Canonical names = **TradingView names** — the permanent standard. To add a pair (e.g. USDMXN): add one line to `config/symbols.json` and restart. No code change.

Every gate now derives from that file: `layer1/main.py ALLOWED_PAIRS`, `layer1/news_filter.py _TICKER_CURRENCIES`, `layer2/state.py ALLOWED_PAIRS/_TICKER_CURRENCIES` all import from `layer2.symbols`. (`config/allowed_pairs.json` was **deleted** — superseded.)

**Broker translation is isolated to Layer 3** (`layer3/symbol_mapper.py`): it discovers each broker's MT5 symbol name from `mt5.symbols_get()` at startup (`EURUSD`→`EURUSD.a`/`.pro`/`m`/…), validates every canonical, caches per-account at `config/symbol_cache_<login>.json`, and refuses cross-currency matches (USDCNY never maps to USDCNH). Missing symbols log `[ERROR]` at startup and show in **`/checksymbols`** (per-broker SUPPORTED/FOUND/MISSING). `config/symbol_map.json` is now an optional manual-override file (canonical→broker), empty by default. Layers 1/2 never see a broker suffix.

> **Two gates, by design:** the registry *opens* the system to a pair; the **TradingView alert** is the real on/off switch (no alert → no signal → no trade). Only arm an alert for a pair `/checksymbols` shows FOUND on the broker that trades it — otherwise the signal dies at Layer 3 execution. Most exotic/NDF/pegged tier (USDIDR, USDVND, USDPKR, USDLKR, USDBDT, USDCNY, USDSAR/AED/QAR, …) will report MISSING on retail/prop MT5; that is expected, not a bug.

`pip_type`: `"jpy"` for USDJPY, `"standard"` otherwise (display only — lot sizing reads live `contract_size`/`tick_value` from MT5, so it generalises to any pair). Price-formatting helpers still recognise metals/indices as a harmless superset for historical records. The Layer 0 `.pine` + TradingView chart/alert set are managed on TradingView, not in-repo.

---

## Hard Constraints

- Personal account always trades **opposite** direction to prop firm.
- Lot sizing uses `baseline_equity × 0.67%` — never live equity. Full formula: TECHNICAL.md §Lot Sizing.
- **`baseline_equity` is the prop-only RISK ANCHOR** — the single value that drives lot sizing (`baseline_equity × 0.67%`) and **every** kill level (K1–K5). Personal lots are derived as `prop_lots × phase_multiplier` (0.20 Phase 1 / 0.70 Phase 2); the personal account has **no** kill conditions and no risk baseline. Immutable — written only by deliberate operator action: `/changepropfirm`, `/phase2`, or `/setbaseline <amount>` (prop-only, no account arg). Never auto-set from MT5 balance.
- **`prop_initial_deposit` / `pers_initial_deposit` are the actual capital** in each account, used **only** for equity-% reporting and the trading-fee reconciliation in `/equity`. They have **zero** effect on lot sizing or kills. Set via `/setdeposit <prop|personal> <amount>`. (Legacy `pers_baseline_equity` is read as a fallback for `pers_initial_deposit`.) Keeping deposit separate from the risk baseline lets a manual run-up before bot handover show in reporting without distorting risk.
- **Personal account currency is whatever MT5 reports** — auto-detected via `_query_equity()` reading `account_info().currency`. Currently **SGD** on the live Fusion Markets account (decision reversed 2026-05-23; was previously USD per `docs/Account_Currency_Decision.md`). The Telegram message layer (post-`b7da59a`, 2026-05-29) renders all personal-side money (Risk/Reward/P&L/Commission/Margin/Equity) in that currency — switching to GBP/EUR/etc. requires no code changes. Forex prices (Entry/SL/TP) carry no currency symbol since they're quotes, not money amounts. **Lot-sizing math is unchanged** — risk is computed from prop equity × `PROP_RISK_PCT`, then converted via `_msg_split_pers_amount(ticker, value, usd_to_acct_rate)` purely for display.
- **Prop account MUST stay USD-denominated** — prop-firm hard constraint. All prop-side money in alerts hardcoded `$`. Phase 2 kill thresholds, baselines, profit targets all denominated in USD.
- **If Warren re-asks the SGD/USD question** — point him at the memory file `~/.claude/projects/.../sgd-usd-account-currency.md` (2026-05-23 reversal) and the Layer 2 retrofit shipped 2026-05-29. Do NOT re-derive from `docs/Account_Currency_Decision.md` — that doc captures the 2026-05-19 decision which has since been overridden.
- Phase switching: Telegram-only (`/phase1`, `/phase2`).
- **MT5 connection (Layer 3):** the `MetaTrader5` lib only gets IPC for a terminal **it self-launches** via `mt5.initialize(path)`. Runtime account switching (creds in `initialize()` or `login()` off the saved default) kills the pipe → `-10005`. **A terminal whose generic install has no broker server endpoints configured will silently never even attempt to connect** — Journal stays empty when you select that account, bottom-right shows `n/a` / `0/0 Kb`, prices appear "frozen" at stale values. This was the multi-week 2026-05 blocker (NOT funding/feed-side, as wrongly diagnosed earlier — both accounts streamed on mobile fine). Fix = follow one of the two workflows in **VPS MT5 Setup** below. Code enforces hard guard `account_info().login == MT5_LOGIN` (fatal exit on mismatch, never trades on the wrong account).
- ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open between VPS #1 and VPS #2/#3.
- TradingView Premium required for webhook delivery.
- One TradingView chart per instrument — 7 charts, 7 pairs.
- Demo-first mandatory: ≥7 trading days before live capital.

---

## VPS MT5 Setup (one-time per account — the workflow that wasted weeks)

> Full debugging journey + diagnostic checklist + what NOT to chase next time: `docs/MT5_VPS_Connection_Postmortem.md`.

**Success signal: bottom-right of MT5 turns green + shows a data rate (e.g. `22.0/0.0 Mb`) AND prices in Market Watch are ticking.** If still "n/a" or "0/0 Kb" after login, the connection is dead, not just slow — try the other workflow option below.

### Option 2 (RECOMMENDED — try this first; desktop-only)

Use the generic MetaQuotes MT5 (from metaquotes.com) + the **Open an Account** wizard to add the broker as a "company". This wires the correct server endpoints into the existing install — no new download needed.

1. Open the existing generic MT5 on the VPS
2. **File → Open an Account**
3. On the "List of companies" page that pops up — **THIS is the step that was missed for weeks** — select the broker's company name (or type its domain in "Find your company"):
   - **Fusion Markets** → choose **`Fusion Markets Pty Ltd`** (3rd entry, as of 2026-05-26)
   - **FundingPips** → choose **`FundingPips Corp (2)`** (2nd entry — the `(2)` matches server `FundingPips2-SIM`)
4. Click **Next** → choose **"Connect with an existing trade account"**
5. Enter login + password → select matching server from dropdown → **TICK "Save password"** → **Finish**
6. Wait until prices stream (bottom-right green + ticking)
7. Close MT5 — the worker will self-launch its own instance

> **Note:** Option 2 is the laptop/desktop workflow. The iPhone MT5 app handles broker selection differently — that path is unrelated and was not what got blocked.

### Option 1 (use only if Option 2's company isn't in the list)

Download the broker's own MT5 installer from their portal:
- **Fusion Markets:** https://fusionmarkets.com/Platforms/Metatrader-5 → MT5 for Windows
- **FundingPips:** log in to fundingpips.com → dashboard → Platforms / Downloads → MT5 for Windows

Install (will go into a folder like `C:\Program Files\Fusion Markets MetaTrader 5\` — note the exact name). Then File → Login to Trading Account → enter creds → TICK "Save password" → Login → wait for green → close.

### Diagnosing failure via the Journal tab (always check first)

| Bottom-right | Journal entries when account selected | Diagnosis |
|---|---|---|
| Green + kb/s + ticking | `authorized on … through Access Point …` | ✅ Done |
| `n/a` / `0/0 Kb` | **ZERO Network entries** | Server endpoints not configured → do Option 2 (or Option 1 if company missing) |
| `n/a` | `authorization failed` | Wrong password or wrong server name |
| `n/a` | `no connection` after `scanning network` | IP-blocked from this VPS → contact broker support |

### Deploying the worker after MT5 is green

1. `git pull` on the VPS for latest Layer 3 connection code
2. `.env` → `MT5_LOGIN` MUST match the MT5 saved-default account (the hard guard refuses mismatches)
3. `.env` → set `MT5_TERMINAL_PATH` ONLY when multiple MT5 installs exist on the same VPS (e.g. both generic and a broker-branded one). VPS #2 example with Fusion-branded installed:
   ```
   MT5_TERMINAL_PATH=C:\Program Files\Fusion Markets MetaTrader 5\terminal64.exe
   ```
   If only the generic MT5 is installed (e.g. typical VPS #3), leave blank — glob `C:\Program Files\*MetaTrader*\terminal64.exe` finds the single install.
4. Close all MT5 windows (worker self-launches its own)
5. `cd C:\arbitrage && uv run python layer3/worker_personal.py` (or `worker_prop.py`)
6. Expect: `MT5 connected — account=<MT5_LOGIN>  server=…  balance=…  mode=…`

---

## Where to look in TECHNICAL.md

| Working on… | Read TECHNICAL.md section |
|---|---|
| Risk math / lot sizing | §Immutable Risk Math |
| Kill conditions K1–K5 | §Kill Conditions (K1 dynamic, K2/K3/K4 static, K5 Phase 2) |
| SGT trading window / curfew | §Trading Window |
| Layer 3 / MT5 / order execution | §Layer 3 — Execution Workers, §MT5 Gotchas |
| Telegram alert formats (Trade Opened / Closed) | §Telegram Alert Formats |
| Trade journal pipeline | §Trade Journal Architecture |
| Config file fields | §Config Files |
| Deployment / `/update` internals | §Deploying Code Changes |
| Pre-live checklist | §Deployment Gates / §Go-Live Checklist |

---

## Current State (as of 2026-06-07)

### Session 21 — Per-pair dedup gate (multi-indicator) — COMMITTED `b6f34b4`, push PENDING (GitHub unreachable), then `/update layer2`

- **Multiple TradingView indicators now fire the same pairs** (e.g. `layer0/Flipped RSI Divergence Indicator.pine` + `layer0/Nadaraya-Watson Webhook INDICATOR.pine`). Both pine files were **verified to emit all 14 webhook fields** the Layer 2 `SignalPayload` requires — no pine changes needed.
- **New per-pair dedup gate in Layer 2** (`logic_core.py`, in `receive_signal` just above the max-positions gate): if the prop account already holds a position on the signal's ticker, the signal is **dropped** (`rejected / position_already_open`) and `msg_signal_skipped_already_open` fires (dedup'd 30 min/pair). Reuses the existing `_query_positions(ZMQ_REQ_PROP)` call. Only the FIRST signal for a pair opens a trade; later dupes wait until it closes. The pine `in_trade` memory is per-indicator only, so this cross-indicator dedup HAD to live in Layer 2. Memory: [[multi-indicator-dedup]].
- **Direction model clarified** (Warren corrected me): the signal's direction IS the personal leg (personal follows signal); prop is the inverse hedge. Prop drives the MATH (lots/kills), not the direction. Memory: [[signal-direction-is-personal]].
- Tests **112 pass**. Files: `layer2/logic_core.py`, `layer2/telegram_handlers.py` (added `msg_signal_skipped_already_open`).
- **Action for Warren:** push `b6f34b4` when GitHub is reachable (`git push origin main`), then `/update layer2`. No `pyproject.toml` change.

### Session 20 — Self-healing guard for degenerate $0 NO_MONEY order_check — SHIPPED to `main`, pending `/update layer3` (prop)

Commit: self-heal guard + 5 tests in `_build_order_check_reply` (`layer3/_worker_core.py`, `tests/layer3/test_order_check_reply.py`). Tests **112 pass**.

- **Session-19 "dual-session GUI" theory DOWNGRADED to unproven.** Warren pushed back: he has run two MT5 GUIs open before with trades filling fine (so a second GUI alone isn't sufficient), and the diagnostic log wasn't even deployed at the failure → **zero log evidence**. Root cause of the $0 NO_MONEY remains unconfirmed (could be launch-time race, feed-reconnect blip, or login contention). Pre-flight `order_check` runs **once, no re-query** (`logic_core.py:1560`) — a single `reject` blocks both legs.
- **Self-healing guard shipped (root cause now moot):** on a NO_MONEY reject whose `order_check margin_free == 0.0` *exactly* (degenerate signature; a real shortfall returns NEGATIVE margin_free), the worker cross-checks LIVE `account_info().margin_free` vs an independent `mt5.order_calc_margin()`. Affordable → downgrade reject→transient so it proceeds/retries instead of killing the trade. Fail-safe: any error or genuinely-broke account keeps the reject. See [[mt5-python-integration-constraints]] pt 8.
- **Account numbers corrected** (Warren changed accounts): personal now `448196`/`FusionMarkets-Live` **6,500 SGD** (was 459166); prop `20047930`/`FundingPips-SIM1` $50k (unchanged since s19). The MT5 terminal saved-default login (+ matching `.env MT5_LOGIN`) is the source of truth for which account trades — NOT these docs. New memory [[trading-account-source-of-truth]]. **Verify VPS #2 `.env MT5_LOGIN=448196`** or the worker fatal-exits on the guard.
- **Action for Warren:** `/update layer3` (2=Prop) + Ctrl+C/re-run `worker_prop.py` (also picks up the s19 diagnostic). Confirm VPS #2 `.env` matches 448196. Then `/resume`+`/rearm`, watch next signal via Telegram.

### Session 19 — Prop XAUUSD "Signal Not Placed" diagnosed + order_check diagnostic log — SHIPPED to `main`, pending `/update layer3` (prop)

Commit: diagnostic `logger.info` in `_build_order_check_reply` (`layer3/_worker_core.py`).

- **New prop account in use:** `MT5_LOGIN=20047930` on server `FundingPips-SIM1` ($50k demo) — **replaces the old `12250900` / `FundingPips2-SIM`**. .env on VPS #3 confirmed. (Old `12250900` references elsewhere in this file are historical.)
- **A prop XAUUSD LONG-hedge signal was rejected "Signal Not Placed":** prop `order_check` returned **NO_MONEY (10019)** with `Needs $0.00 margin / Free $0.00` on the $50k account. Diagnosed as a **bogus/degenerate read, NOT a real shortfall** — Phase-1 gold lot ≈ 0.27 lots needs ~$4-7k vs $50k free; not lot-too-big, not narrow-SL (→10016), not wrong account. Original session-19 leading theory (interactive MT5 GUI left logged in → zeroed `account_info().margin_free`) is **unconfirmed — see Session 20 above, it was downgraded**. Both legs gate together, so the bogus prop reject also suppressed the (fine) personal leg. Memory: [[mt5-python-integration-constraints]] (point 8).
- **Fix added:** `_build_order_check_reply` now logs `margin_req/free/bal/eq` + a live `account_info` `login/free` cross-check (defensive try/except, non-fatal). Next $0 reject will show whether `account_info free=50000` while `check free=0` (→ confirms dual-session theory). Tests **107 pass**.
- **Action for Warren:** close the desktop MT5 on VPS #3, `/update layer3` (2=Prop) + Ctrl+C/re-run `worker_prop.py`, then `/resume`+`/rearm` and watch the next signal. Monitor via Telegram, not the desktop GUI.

### Session 18 — Knowledge base built + full correctness audit + `/phase1` fee-anchor reset — SHIPPED to `main`, pending `/update layer2`

Commits: `7773771` (KB + folder reorg), `b51af15` (audit cleanups), `f2f92e5` `d57c1da` (KB notes), `98e3709` (`/phase1` fee reset).

- **Knowledge base built** at `docs/reference/` (`index`, `architecture`, `calculations`, `messages`, `execution`, `deployment`) from a code-verified file-by-file pass. **Consult it first** to locate file:line, then act; keep it in sync on every code change. CLAUDE.md now leads with a "🧠 Knowledge base — CONSULT FIRST" block.
- **Folder reorg done** — the `accd561` deletion table is fully cleared (already mostly gone; removed empty `*.log`, de-linked `docs/README.md` from the deleted AI_Workflow.md). Only residue: a gitignored, env-locked root `.DS_Store`.
- **Correctness audit (whole codebase)** — trading math (Phase 1/2 geometry, kills K1–K5, lot sizing), order execution, fee/deal handling, currency formatting all verified **correct**. Only fixes were 3 safe cleanups (`b51af15`): a dead no-op `warn=""` block in `_p1_input`, an unused `pos_str` double-VPS query in the Phase-1 kill branch, and the dead `_set_personal_baseline` fn. Tests 107 pass throughout.
- **Phase 1 geometry** — ⚠️ this session-18 description is **SUPERSEDED by session 22** (2026-06-07). The "growing gap carried by lot size, TP anchored at signal SL" model was **replaced**: Phase 1 is now **FIXED-LOT, moving-TP** — only the signal TP is used (signal SL discarded), the prop is sized over its own stop (lots fixed: gold $1k→1.0 lot, $2k→2.0), and the calculated prop TP carries the gap and becomes the personal SL. RR still 4.5/5.5/6.5 (stage-based) but via the moving TP, not lots. See session 22 + `docs/reference/calculations.md`. Two-risk model unchanged (per-trade risk = `fixed_risk`; K1/K2 = baseline-derived). Phase 2 untouched. Memories: [[phase1-reward-risk-scaling]], [[phase1-phase2-separate-logic]], [[baseline-always-configured]].
- **`/phase1` now resets the per-cycle trading-fee anchor** on both workers (`98e3709`), same as `/changepropfirm` and `/phase2` (Warren's request). Needs `/update layer2`.

### Session 17 — Per-cycle trading-fee anchor + wizard re-entry / `/rearm` + final personal-`$`→SGD — SHIPPED to `main`, pending deploy

Commits: `427828d` (fee anchor + currency), `2a26bad` (allow_reentry + /rearm).

- **Trading Fee is now per-cycle, not since-account-open.** Root cause of the bogus prop `Trading Fee: $+50,000`: the identity `balance − Σ(deal.profit)` assumes the deposit is booked as a balance-type deal; a fresh demo (FundingPips set to $50k, no deposit deal) has `Σprofit=0` → `fee=balance`. Fix: worker persists a **fee anchor** = `(balance − Σdeal.profit)` at cycle start (`config/fee_anchor_<login>.json`, gitignored); `/equity` reports `(residual − anchor)`. The unbooked-deposit offset cancels in the subtraction, so it's correct for both account types. New worker query `reset_fee_anchor`; Layer 2 fires `_dispatch_fee_anchor_reset()` on **both** workers after `/changepropfirm` and `/phase2`. Also widened the fee-scan `to_dt` to `now+1day` (server-tz, same as session 16). **After deploy, prop fee still shows $+50,000 until a reset fires — run `/changepropfirm` or `/phase2` once to capture the anchor.**
- **`/phase1` "no prompt" bug fixed:** no ConversationHandler had `allow_reentry`, so re-sending `/phase1` while already mid-conversation was silently ignored (no prompt reappeared). Added `allow_reentry=True` to all 7 wizards. Stuck-state recovery without deploy: `/cancel` then `/phase1`.
- **New `/rearm` command** (in `/help` under Trading Control): clears `soft_kill_override_day` so today's K1/K3 + Phase 1 stage halt fire again after an accidental `/resume`. Permanent kills (K2/K4/K5) unaffected.
- **Final personal-`$`→SGD:** the last 3 hardcoded `$` offenders (wizard baseline echoes — `/changepropfirm` review, Account Setup Saved, Phase 2 Active) now use `_money(v, await _pers_currency())`. Full audit confirms zero personal-context `$` remain. `$` on prop risk figures (`/pnl`, stages, reward:risk) is correct (prop-USD). Memory: [[telegram-reporting-standards]].
- **Phase 1 reward:risk scales with baseline:** `9000:2000` was for the $100k account; the live $50k account uses `4500:1000` (first_reward must be < target = $5,000). Warren will configure it himself. Memory: [[phase1-reward-risk-scaling]].
- Tests: **107 pass** (no test changes — behavior is config/transport).
- **Deploy:** `/update layer2` (Telegram) + `/update layer3` ×2 (`_worker_core.py` changed). No `pyproject.toml` change.

### Session 16 — Deal-history timezone window fix (journal lag / `(est.)` close alerts) — SHIPPED to `main`, pending Layer 3 deploy

Commits: `884eb02` (retry backoff extend), `4a2222a` (the real fix — `to_dt` window), `855a421` (presentation test).

- **Real root cause found:** `mt5.history_deals_get(from,to)` filters on `deal.time`, which MT5 reports in the **trade server's timezone (≈UTC+2/+3), not UTC**. Both deal-history queries set `to_dt = UTC-now + a few seconds`, so a just-closed deal — stamped 2-3h ahead — fell outside the window and stayed invisible for hours. THAT (not broker lag) is why journaling always queued and close alerts showed `(est.)`, **on the live account too**. The "MetaQuotes Demo lags 2-3h" note in old comments was this same bug misdiagnosed. Memory: [[mt5-deal-history-server-timezone]].
- **Fix (read-window only, zero execution risk):** `to_dt = UTC-now + 1 day` in `layer3/journal/journaling_worker.py::_get_deals` AND `layer3/_worker_core.py::_build_deal_pnl_reply`. Both still filter by exact `position_id` + `DEAL_ENTRY_OUT` → a wide future window can't match a wrong deal. Deal now surfaces on the first query → Layer 2's 30s monitor flushes `msg_position_closed` with real P&L/exit/fee, no `(est.)`, and the journal rarely queues.
- Also extended the Layer 3 inline retry backoff to ~735s (>L2's ~630s close-alert cap) as a backstop so any genuine outage orders "Journal Queued" *after* the close alert, not before.
- **Tests: 107 pass** (+3 new `tests/layer2/test_position_closed_alert.py` pinning the no-`(est.)` presentation contract). `msg_position_closed` text logic was verified correct and **left unchanged** — it already renders real values whenever `deal['found']`.
- **Deploy:** `/update layer3` ×2 (both `_worker_core.py` + `journaling_worker.py` changed). No `pyproject.toml` change. Confirm by closing one trade: alert ≤30s, real P&L, no `(est.)`.

### Session 15 — Universal symbol mapper + TradingView webhook 422 fix — SHIPPED to `main`, pending deploy

Commits: `575af7d` (symbol mapper), `8c77009` (webhook pine + folder cleanup).

- **Single source of truth = `config/symbols.json`** (canonical = TradingView names). Expanded **7 → 33 symbols** (31 FX + XAUUSD + XAGUSD). Loader `layer2/symbols.py` (stdlib-only, imported by L1/L2/L3). Layer 1 `ALLOWED_PAIRS`/`_TICKER_CURRENCIES` and Layer 2 `state.py` now derive from it; `config/allowed_pairs.json` **deleted**. See §Covered Instruments.
- **Broker translation isolated to `layer3/symbol_mapper.py`** — discovers each broker's MT5 name via `symbols_get()` at startup, validates every canonical, caches per-account (`config/symbol_cache_<login>.json`, gitignored), refuses cross-currency matches (USDCNY≠USDCNH). New **`/checksymbols`** Telegram cmd reports per-broker SUPPORTED/FOUND/MISSING. `config/symbol_map.json` is now an empty manual-override file. Two gates: registry opens a pair; the TradingView alert is the real on/off — only arm an alert for a pair shown FOUND.
- Most exotic/NDF/pegged tier will report MISSING on retail/prop MT5 — expected. Tests: **104 pass** (+14 mapper).
- **TradingView 422 fixed:** root cause = AlgoAlpha NW indicator emitted only 6 fields; L1 needs 9, L2 needs 14. Fix shipped via Option B (enrich the Pine payload, schemas untouched) in `layer0/Nadaraya-Watson Webhook INDICATOR.pine` — Warren pastes that into TradingView + recreates the alert. See [[webhook-payload-contract]] (memory) for the full contract + the `str.tostring(na)`→"NaN" 422 trap.

> Env FS constraint hit this session: cannot create new top-level dirs/files or delete root dirs (EPERM). That's why the shared loader lives in `layer2/symbols.py` not `common/`. See memory `repo-fs-write-constraints`.

### Session 14 — Telegram reporting overhaul + baseline/deposit split — SHIPPED to `main`, awaiting deploy on BOTH layers

Pending: `/update layer2` AND `/update layer3` (×2 — `_worker_core.py` changed, so both Personal and Prop workers must restart). No `pyproject.toml` change → no `uv sync`. **Layer 3 trading-fee work was verified live on 2026-05-29 (fee reconciles); HEAD `033b97e` is the version to deploy.**

Commits (chronological): `968f9bb` `f2c02dd` `7495ebd` `783dba1` `95ee73c` `d32f316` `43b1ccd` `d42fde8` `aeb7757` `40203e5` `033b97e` `3d4dbaa`.

⚠️ **Layer 3 workers do NOT pick up new code on `git pull` alone — the Python process must be Ctrl+C'd and re-run.** This caused the trading-fee value to stay wrong across "redeploys" until the worker was truly restarted. Closing/reopening the noVNC tab does not restart it. Confirm via the `FEE DEBUG`/build markers or simply that the value changed.

What shipped:
- **All on-demand command outputs restructured** to the `━` header + `Label: value` format (matching the alert templates) via new helpers `_cmd_header()` / `_cmd_pos_block()` / `_pers_currency()` in `telegram_handlers.py`. (Note: `968f9bb` referenced those helpers before they were defined — broken at runtime; `f2c02dd` defined them. Both are on `main`; HEAD is fine.)
- **Risk baseline vs initial deposit are now separate concepts** (see Hard Constraints): `baseline_equity` = prop-only risk anchor (sizing + kills K1-K5); `prop_initial_deposit`/`pers_initial_deposit` = actual capital for equity-% + fee reporting only. New commands: `/setbaseline <amount>` (prop risk), `/setdeposit <prop|personal> <amount>`. Legacy `pers_baseline_equity` read as fallback for `pers_initial_deposit`.
- **`/equity` "Trading Fee"** (renamed from "Commission") = the all-in cost via the robust identity **`balance − Σ(every deal.profit)`** (Σ profit = deposits + gross realized P&L, since commission/swap live in separate fields). Equivalent to `balance − deposit − gross` and to `Σ(commission)+Σ(swap)`. NOT MT5's commission field alone (under-reports swap). Gated behind a `want_fee` flag so the full-history scan runs ONLY for `/equity`, never the 30s monitor poll. Verified live: personal −SGD 6.01, prop −$8.98 (2026-05-29).
- **Close-alert wrong-P&L bug FIXED** (`d42fde8`): `_build_deal_pnl_reply` now matches the realized deal by the exact closed-position `ticket` (`position_id`), not symbol+latest-exit — which previously paired one ticket's metadata with another trade's P&L when multiple same-symbol trades closed / MetaQuotes history lagged. If the ticket's deal hasn't surfaced, returns `found=False` → shows `(est.)` rather than a wrong number. Close alert also shows "Trading Fee" (commission+swap) not "Commission".
- Verified fact (this session): lot sizing + ALL kills are PROP-only; the personal account has no kill conditions and its lots = `prop_lots × phase_multiplier`. The personal baseline was always cosmetic.
- **Trading list trimmed 8→7** (`3d4dbaa`): dropped XAGUSD + NAS100/USTEC from every gate (see §Covered Instruments). Trading Fee is fully dynamic (live `account_info` + `history_deals_get`), no static constants — verified.
- Tests: 90 pass. Updated stale `test_buffers.py` to the shipped 1pp daily-DD buffer.

> A rewind mid-session reverted working files behind git HEAD; recovered via `git restore`. If files ever look older than `git log`, that's the cause — `git restore <files>` to resync to `origin/main`.

### Live trading state — UNBLOCKED, awaiting VPS deploy

Both VPS desktops stream live broker data and the Layer 3 connection rewrite is shipped. Awaiting deploy on the VPSes.

**What unblocked it (the actual root cause, after weeks of wrong diagnoses):** the generic MetaQuotes MT5 (downloaded from metaquotes.com) does NOT bake in broker server endpoints. When you select "FusionMarkets-Live" or "FundingPips2-SIM" in such an install, the terminal silently never even attempts a connection — Journal shows zero Network entries, bottom-right reads `n/a`/`0/0 Kb`, prices stay frozen at stale values. The MetaQuotes-Demo built-in account streams fine in the same install, which masked the real issue. **Both accounts were funded and streaming on mobile the whole time.** Fix is purely server-endpoint config — see **VPS MT5 Setup** section above.

**Layer 3 code state (`main` HEAD):**
- `72b3921` — `_worker_core._connect_mt5()` rewritten to self-launch via `mt5.initialize(path)` + hard guard `account_info().login == MT5_LOGIN`
- `75f55f5` — terminal-path glob broadened to `C:\Program Files\*MetaTrader*\terminal64.exe` so broker-branded installs (e.g. `Fusion Markets MetaTrader 5\`) are found

**Verified-streaming state on the VPSes (2026-05-26):**
- VPS #2 (personal): Fusion-branded MT5 + generic MT5 both installed; saved default is now **`448196` ("Chee Heng Lai 006") on `FusionMarkets-Live`** — **SGD-denominated (6,500 SGD)**, account changed 2026-06-04 (was `459166`/486 SGD earlier). VPS `.env` `MT5_LOGIN` must equal this or the worker fatal-exits.
- VPS #3 (prop): generic MetaQuotes MT5; saved default is **`20047930` on `FundingPips-SIM1`** ($50k demo, USD) — the prop account as of session 19 (was `12250900`/`FundingPips2-SIM` $5k earlier). The source of truth for which account trades is the MT5 terminal's saved-default login (+ matching `.env` `MT5_LOGIN`), NOT these docs — see [[trading-account-source-of-truth]]. The session-19 dual-session→degenerate-`order_check` link is an **unconfirmed theory** (Warren ran two GUIs open before with no issue); no diagnostic was live at the failure so it's unproven.

**Layer 3 deploy steps (one-shot, both VPSes):**
1. `cd C:\arbitrage && git pull` on both VPSes
2. VPS #2 only: edit `.env` → add `MT5_TERMINAL_PATH=<path to Fusion-branded terminal64.exe>`. Required because two MT5 installs coexist there.
3. VPS #3: no `MT5_TERMINAL_PATH` needed (only one MT5 install)
4. Close all MT5 windows (worker self-launches), then `uv run python layer3/worker_{personal,prop}.py`
5. From Telegram: `/health` → both legs green
6. Housekeeping: delete `C:\arbitrage\config\mt5_autologin.ini` if it still exists (leftover plaintext-password file, unused)

### Layer 2 telegram-message consolidation + 20-37 retrofit (2026-05-28 → 2026-05-29) — SHIPPED, awaiting `/update layer2`

All Telegram message text moved out of `logic_core.py` into named `msg_*()` functions in `telegram_handlers.py`. Each function has a docstring describing its trigger condition. Two new Telegram commands print the catalog (`/messages` for templates 1-19, `/messages2` for 20-37). All 37 templates redesigned with Warren's header + ━ separator + labeled-block format. **Audit confirmed zero orphans** — every message function is wired to at least one logic_core call-site.

Templates 20-37 received an additional layout retrofit on 2026-05-29: `Label: value` rows replace space-padded alignment; tickets render `#<id>` directly under each side header; personal-side money renders in the MT5-reported account currency (auto-detected — SGD on the live account); prop stays `$`; forex prices stay raw.

**Layer 2 commits (chronological):**
- `5d1f58a` — consolidate Telegram messages + `/messages` catalog
- `1b03ddf` — paginate `/messages` so all 37 templates survive Telegram flood control
- `8940848` — apply Warren's redesigned templates 1-19
- `4f7a69b` — apply redesigned templates 20-37 + audit
- `b7da59a` — msgs 20-37: `:`-separated rows, `#` ticket prefix, single-currency personal display
- `c212ce5` — msgs 20-37: move ticket row directly under each side header

**Deploy:** `/update layer2` in Telegram, then `/messages` and `/messages2` on phone to review.

### Next session

Read `docs/SESSION_HANDOFF.md` — it carries the in-flight delta. Lower-priority queued items still open: (A) retrofit msgs 1-19 with the three 20-37 layout fixes, (B) deep discussion with Warren on overall message structure + write a one-paragraph spec, (C) fold (B)'s spec back into CLAUDE.md. Folder reorganization (from the prior handoff at `accd561`) is still queued at lower priority.
