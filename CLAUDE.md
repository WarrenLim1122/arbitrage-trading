# CLAUDE.md

Lean router for Claude Code. Reference-shaped detail lives elsewhere — load only what the task needs:

- **`docs/reference/` (start at `index.md`)** — code-verified KB: architecture, calculations (risk/lot/Phase 1+2 geometry/kills), Telegram messages, Layer 3 execution, deployment. **Consult it FIRST to locate the exact file:line, then act. Keep it in sync in the same session as any code change.** Memory: [[knowledge-base-workflow]].
- **`TECHNICAL.md`** — risk math, kill formulas, layer deep-dives, MT5 gotchas, Telegram formats, deployment gates, go-live checklist (routing table at the bottom of this file).
- **`docs/SESSION_HANDOFF.md`** — the in-flight delta. **Read it the first time Warren returns each session.**
- **`docs/SESSION_LOG.md`** — archived per-session changelog (sessions 14–22). History only; read for the "why" behind a past change.
- **`docs/VPS_MT5_Setup.md`** — one-time MT5 connect workflow per account. **`docs/MT5_VPS_Connection_Postmortem.md`** — full debugging journey.

---

## Workflow Rules

- **Auto-push to GitHub after every code change.** Warren has standing permission for all pushes to `main`. Commit and push immediately after any file edit — never wait to be reminded.
- **After a push, tell Warren which Telegram `/update` command to run** (don't repeat full deploy steps):
  - Layer 1/2 changes → `/update layer2`
  - Layer 3 changes → `/update layer3` (1 = Personal, 2 = Prop)
  - `uv sync --extra layer3` only if `pyproject.toml` changed (mention explicitly if so).
- When Warren asks how to deploy: if `/update` covers it, name the subcommand; if not, debug first, then ask whether to add it to `/update`.
- **Layer 3 workers do NOT reload on `git pull` alone** — the Python process must be Ctrl+C'd and re-run. Closing/reopening noVNC does not restart it. See [[mt5-python-integration-constraints]].
- **`/update layer2` can abort on `git pull` if a commit touches a runtime-mutated `config/*.json`** (`propfirm_config.json`, `phase_config.json`, `consistency_log.json`, `risk_params.json` are tracked but rewritten live on the VPS). Error: *"local changes would be overwritten by merge."* This is NOT uncommitted-local-edits — it's the dirty VPS tree. Unblock = backup→`git stash`→`git pull`→restore live config→`git stash drop`→restart. Proper fix (untrack + `.example` seeding) is PENDING Warren's go. [[deploy-runtime-config-conflict]]

---

## Project

Automated Trade Execution Engine — 4-layer cross-hedging system. Personal account (Fusion Markets) follows signal direction; prop firm account (FundingPips) executes the **inverse** as a hedge. Sizing is phase-dependent, controlled via Telegram.

```
TradingView (15m chart — one per pair)
  └── layer0/1D-15m Breakout INDICATOR.pine    [HTTPS webhook]
  layer1/main.py          (VPS #1, port 8000 — public)      [internal HTTP]
  layer2/logic_core.py    (VPS #1, port 8001 — internal)    [ZeroMQ PUSH]
        ├── layer3/worker_personal.py  (VPS #2, Windows)
        └── layer3/worker_prop.py      (VPS #3, Windows)
Telegram Bot API ←→ layer2/logic_core.py
```

## Infrastructure

| VPS | Provider | IP | OS | Purpose |
|---|---|---|---|---|
| VPS #1 | DigitalOcean (SGP1) | 152.42.213.98 | Ubuntu 24.04 | Layer 1 + Layer 2 + nginx + TLS |
| VPS #2 | Vultr | 139.180.136.233 | Windows Server | worker-personal (Fusion Markets MT5) — `C:\arbitrage` |
| VPS #3 | Vultr | 45.76.156.55 | Windows Server | worker-prop (FundingPips MT5) — `C:\arbitrage` |

- **Public endpoint:** https://api.warrenlimzf.com/signal · **Telegram bot:** HedgeHog (token in VPS #1 `.env`)
- **noVNC:** VPS #2 `…novnc/?id=6288e88e-1ad6-468a-a584-914bd04590b1` · VPS #3 `…novnc/?id=88dfe741-382d-47fe-a19c-199baa534bfc`
- **Billing:** DigitalOcean end-of-month · Vultr prepaid credit (Visa 7119 auto-charges).
- VPS #1 layers = systemd (auto-restart). VPS #2/#3 workers run in PowerShell — manually restart after VPS reboot. Don't close the PowerShell window; closing the noVNC tab is safe.

## Build Status

| Layer | Files | Status |
|---|---|---|
| 0 — Signal Engine | `layer0/*.pine` | ✅ LIVE — 7 alerts active. **Frozen — do not edit without asking Warren first.** |
| 1 — Gatekeeper | `layer1/main.py`, `news_filter.py`, `ff_calendar.py` | ✅ LIVE — systemd on VPS #1 |
| 2 — Logic Core | `layer2/logic_core.py`, `telegram_handlers.py`, `state.py` | ✅ LIVE — Phase 1 = fixed-lot/moving-TP, Phase 2 = full-signal box (different geometries; see `docs/reference/calculations.md`). |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ Live cutover unblocked — both VPSes stream live. Self-launch + hard account guard. |

> Current deploy/pending state is in `docs/SESSION_HANDOFF.md`, not here.

## Covered Instruments

Single source of truth: **`config/symbols.json`** (canonical = TradingView names), loaded by `layer2/symbols.py`. Add a pair = add one line + restart, no code change. All Layer 1/2 gates derive from it. Broker translation is isolated to Layer 3 (`layer3/symbol_mapper.py`, per-broker discovery + cache). **Two gates:** the registry *opens* a pair; the **TradingView alert** is the real on/off switch. Only arm an alert for a pair `/checksymbols` shows FOUND on the trading broker — exotic/NDF/pegged reporting MISSING is expected, not a bug. Full detail: `docs/reference/architecture.md` + [[checksymbols-and-pair-registry]].

---

## Hard Constraints

- Personal account always trades **opposite** direction to prop. The signal's direction = the personal leg; prop is the inverse hedge and drives the MATH (lots/kills), not the direction. [[signal-direction-is-personal]]
- **Lot sizing uses `baseline_equity × 1.0%` (Phase 2; raised from 0.67% 2026-06-14) — never live equity.** `baseline_equity` is the **prop-only risk anchor** driving sizing + every kill (K1–K5). Personal lots = `prop_lots × phase_multiplier` (0.20 Phase 1 / 0.70 Phase 2); personal has **no** kills and no baseline. Immutable except via `/changepropfirm`, `/phase2`, `/setbaseline <amount>`. Never auto-set from MT5 balance. Formula: TECHNICAL.md §Lot Sizing.
- `prop_initial_deposit` / `pers_initial_deposit` = actual capital, used **only** for equity-% + trading-fee reporting in `/equity` — zero effect on sizing/kills. Set via `/setdeposit <prop|personal> <amount>`.
- **Personal account currency = whatever MT5 reports** (auto-detected; currently **SGD** on Fusion Markets — 2026-05-23 reversal). All personal-side money renders in that currency; switching needs no code change. Forex prices carry no currency symbol. If Warren re-asks SGD/USD: point at memory [[sgd-usd-account-currency]] + the 2026-05-29 Layer 2 retrofit — do NOT re-derive from `docs/Account_Currency_Decision.md` (superseded).
- **Prop account MUST stay USD-denominated** (prop-firm hard constraint). All prop-side money hardcoded `$`.
- Phase 1 vs Phase 2 are **separate geometries by design** — never unify (attempt reverted 2026-06-07). [[phase1-phase2-separate-logic]], [[phase1-reward-risk-scaling]].
- Phase switching: Telegram-only (`/phase1`, `/phase2`).
- **MT5 connection (Layer 3):** the lib only gets IPC for a terminal **it self-launches** via `mt5.initialize(path)`; runtime account switching kills the pipe (`-10005`). A generic install with no broker server endpoints configured silently never connects. Hard guard `account_info().login == MT5_LOGIN` (fatal exit on mismatch). Fix workflow: `docs/VPS_MT5_Setup.md`. [[mt5-python-integration-constraints]], [[trading-account-source-of-truth]].
- ZeroMQ ports 5555 (PUSH/PULL) + 5556 (REQ/REP) open between VPS #1 and VPS #2/#3.
- TradingView Premium required for webhooks. One chart per instrument (7 charts, 7 pairs). Demo-first: ≥7 trading days before live capital.

---

## Parked idea — AI-signal stress test (DEFERRED, do not auto-start)

Warren wants to eventually test whether LLM-agent strategies have any real edge, using THIS harness as the validation rig (it already owns execution + risk + demo accounts + Telegram control).

- Two research repos are cloned at `~/.agents/external/`: **`ai-hedge-fund`** (virattt — LLM agents impersonating famous investors) and **`TradingAgents`** (TauricResearch — multi-agent analyst/trader/risk debate). Both are **signal generators**, educational-only, with **no verified live edge**; any README return/win-rate figure is an overfit backtest. Hedging reduces variance, not the absence of edge — so combining them is not automatic alpha.
- **Plan when capital allows:** feed one repo's signals into the existing Layer 0→2 webhook in **demo/paper only**, run for weeks, and measure *its own* expectancy-after-costs / Sharpe / win-rate on Warren's instruments — i.e. generate the statistics that don't exist, instead of trusting a README. Only graduate to small real capital if it clears a pre-set out-of-sample bar.
- **Reminder duty:** this is a queued idea, NOT a trigger. Surface it once if relevant ("Warren — the AI-signal stress test is still parked; want to start it?"), then wait for him to explicitly ask. Don't begin wiring it unprompted.

---

## Where to look in TECHNICAL.md

| Working on… | Section |
|---|---|
| Risk math / lot sizing | §Immutable Risk Math |
| Kill conditions K1–K5 | §Kill Conditions |
| SGT trading window / curfew | §Trading Window |
| Layer 3 / MT5 / order execution | §Layer 3 — Execution Workers, §MT5 Gotchas |
| Telegram alert formats | §Telegram Alert Formats |
| Trade journal pipeline | §Trade Journal Architecture |
| Config file fields | §Config Files |
| Deployment / `/update` internals | §Deploying Code Changes |
| Pre-live checklist | §Deployment Gates / §Go-Live Checklist |
