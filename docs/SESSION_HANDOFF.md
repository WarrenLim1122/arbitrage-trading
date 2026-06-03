# Session handoff — NEXT SESSION = build the knowledge base (learn → understand → write)

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single Claude agent + Warren (operator). Warren runs all Telegram `/update` deploys and VPS actions himself; agent edits code on `main` and pushes.

## Status — updated 2026-06-03
- Phase 1 calculation logic was reviewed in depth this session and **confirmed correct** — no code change. See memory [[phase1-reward-risk-scaling]] for the exact conventions (reward:risk = PROP perspective; $50k = half of $100k; risk fixed per trade, RR 4.5→0.25 intended; `min_days × risk = 6% overall DD`; stages set at setup, final stage = the 10% line).
- SL/TP/lots mechanics were fully traced (`layer2/phase1_strategy.py`, `layer2/phase2_strategy.py`): **Phase 2** = personal SL+TP both fixed from signal, lots from `baseline×0.67%`; **Phase 1** = personal SL fixed from signal, **TP calculated** (signal TP ignored), lots from the stage reward-gap. This is documented only in the chat — needs to go into the KB.
- Code shipped earlier this session (already on `main`, still **not deployed**): per-cycle fee anchor, `allow_reentry` on all wizards, `/rearm`, final personal-`$`→SGD. See CLAUDE.md §Current State → Session 17.

## Next actions
1. **PRIMARY — build the knowledge base ("brain").** Warren wants a heavyweight learn→understand→write pass so future sessions read references first instead of re-reading every code file. Do this:
   - Read the project **file by file** (layer0 → layer1 → layer2 → layer3 → config → tests). Understand the full architecture and data flow.
   - Write authoritative reference docs under **`docs/reference/`** (env blocks NEW top-level dirs per [[repo-fs-write-constraints]] → keep the KB inside the existing `docs/`). Suggested files:
     - `architecture.md` — layers, data flow, ZMQ/HTTP wiring, VPS map.
     - `calculations.md` — risk math, lot sizing, Phase 1 vs Phase 2 stage + SL/TP geometry (capture the table from this session), kill conditions K1–K5, the `min_days×risk = 6% DD` invariant.
     - `messages.md` — the `msg_*()` catalog + `_cmd_*`/`_cmd_header`/`_MSG_SEP` formatting standard, currency rules (prop `$` / personal account-currency).
     - `execution.md` — Layer 3 / MT5 connection, symbol mapper, journaling, fee anchor.
     - `deployment.md` — `/update` subcommands, worker-restart gotcha, deploy gates.
     - `index.md` — map of the KB + the rule "consult these before editing code; update them when code changes."
   - Cross-check against existing `docs/` (Project_Overview.md, System_Architecture.md, MT5_VPS_Connection_Postmortem.md, TECHNICAL.md) and CLAUDE.md — consolidate, don't duplicate; link to TECHNICAL.md where it already covers a topic.
   - **Verify claims against the actual code as you write** — do not assert math/file:line from memory.
2. **Carry-over deploy (Warren does this):** `/update layer2` + `/update layer3` ×2 (sessions 15–17). Then `/checksymbols`; close one trade (alert ≤30s, real P&L, no `(est.)`); run `/changepropfirm` or `/phase2` once to capture the fee anchor (else prop `/equity` shows bogus `$+50,000`). Start Phase 1: `/phase1` → `4500:1000` → `CONFIRM`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (work on `main`)

## Open items
- Account switch on VPS #2 (personal): Warren toggled `.env`/MT5 between `448196` and `459166` — confirm which the worker finally bound to via the `MT5 connected — account=…` line or `/health` after deploy.
- Tests: 107 pass. Local configs (`config/phase1_config.json`, `propfirm_config.json`) are empty — live values are on the VPS, so KB examples should use the documented $50k/10%/3-day account, not local config.

## Pick up here
Start the knowledge-base build: read file-by-file and write `docs/reference/` (architecture, calculations, messages, execution, deployment, index). Tell Warren the KB is the persistent "brain" he asked for, and that from now on you consult it first and keep it in sync with code.
