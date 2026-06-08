# Session log — archived changelog (sessions 14–22)

> Historical session-by-session record, moved out of `CLAUDE.md` (2026-06-08) to keep that file a lean router.
> The **in-flight delta** lives in `docs/SESSION_HANDOFF.md`; durable facts live in the memory files and `docs/reference/`.
> This file is append-only history — read it only when you need the "why" behind a past change.

## Current State (as of 2026-06-07)

### Session 22 — Phase 1 rewritten to FIXED-LOT / moving-TP + project-wide consistency + 2 bug fixes — SHIPPED to `main`, pending `/update layer2`

Commits: `5f719fe` (model + consistency pass), `b0a98c5` (bug fixes). Also `993ed31`→`3132fb9` (a wrong "unify P1 into P2's box" attempt + its revert — net zero, ignore).

- **Phase 1 geometry is now FIXED-LOT, moving-TP** (`layer2/phase1_strategy.compute_geometry`). Warren's exact spec: the signal is for PERSONAL, prop inverts. **Only the signal TP (near level) is used — the signal SL is DISCARDED.** prop SL = signal TP; prop sized over that stop → `lots_prop = fixed_risk / (|signal_tp−entry| × k)` so **lots are FIXED** (gold $1000 risk → 1.00 lot; $2000 @ $100k → 2.00). prop TP = **calculated** to win the stage gap (`reward_gap / (lots × k)`) and **becomes the personal SL** (clean mirror box). RR = reward_gap/fixed_risk → **4.5 → 5.5 → 6.5** over a losing run (gap +$1000/loss), resets to ~**0.25** right after a stage win. Nothing hardcoded (k live from MT5, risk+ratio from config). Memories: [[phase1-reward-risk-scaling]], [[phase1-phase2-separate-logic]].
- **Phase 2 unchanged + confirmed correct**: uses ALL signal levels (SL/entry/TP), prop = exact inverse (prop SL=signal TP, prop TP=signal SL), risk = `baseline × 0.67%`, **lots VARY** with the signal TP distance, RR = signal's SL:TP ratio (3.7 for gold). Kills K1–K5 (adds K3 daily-cap + K5 consistency).
- **2 bug fixes** (found via simulation, `b0a98c5`): (1) degenerate prop TP — a sub-precision stage gap rounded prop TP onto entry → reject guard added; (2) zero-tick 500 crash — `pers_*` contract fields now coalesce 0/None to the prop's validated value (`or`, not `.get`-default). Both phases.
- **Consistency pass** so nothing contradicts the model: `TECHNICAL.md §Immutable Risk Math` (was "never change between phases" — now split P1/P2), `docs/reference/calculations.md`, this file, module docstring, the stale "size dynamic reward" guard text.
- Tests **114 pass**. **Action for Warren:** `/update layer2`. To start: `/phase1` → `4500:1000` → `CONFIRM`. Open design Qs (NOT bugs): personal SL risk balloons on a losing streak (personal has no kill); Phase 2 personal ratio still 0.70 (only P1 is ÷5).

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

**What unblocked it (the actual root cause, after weeks of wrong diagnoses):** the generic MetaQuotes MT5 (downloaded from metaquotes.com) does NOT bake in broker server endpoints. When you select "FusionMarkets-Live" or "FundingPips2-SIM" in such an install, the terminal silently never even attempts a connection — Journal shows zero Network entries, bottom-right reads `n/a`/`0/0 Kb`, prices stay frozen at stale values. The MetaQuotes-Demo built-in account streams fine in the same install, which masked the real issue. **Both accounts were funded and streaming on mobile the whole time.** Fix is purely server-endpoint config — see `docs/VPS_MT5_Setup.md`.

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
