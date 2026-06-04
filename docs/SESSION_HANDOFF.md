# Session handoff — KB built, full correctness audit, /phase1 fee-anchor reset

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single Claude agent + Warren (operator). Warren runs all Telegram `/update` deploys and VPS actions himself; agent edits code on `main` and pushes.

## Status — updated 2026-06-04 (session 18)
Shipped this session (commit list in CLAUDE.md §Current State → Session 18):
- **Knowledge base** at `docs/reference/` — the persistent "brain". Consult first, keep in sync.
- **Folder reorg done** (accd561 table cleared).
- **Whole-codebase correctness audit** — no trading-logic bugs; only 3 safe cleanups shipped. Tests 107 pass.
- **Phase 1 geometry confirmed correct** with Warren (lots scale with the gap, not pulled-TP). Phase 2 untouched/correct.
- **`/phase1` now resets the per-cycle fee anchor** on both workers (needs `/update layer2`).

## Next actions (Warren does the deploys)
1. **`/update layer2`** — picks up the session-18 `/phase1` fee-reset (and the earlier sessions 15–17 Telegram changes).
2. **Finish the personal-worker (VPS #2) deploy — this is the live bug right now.** Personal `/equity` shows `Trading Fee: SGD −12.40` because VPS #2 is still on **pre-session-17 code** (no fee-anchor logic): the −12.40 is the full since-open residual with no anchor subtracted, while prop correctly shows `$0`. Both workers run the SAME `_worker_core.py`, so prop-resets-but-personal-doesn't = personal is on a stale build. Fix on VPS #2: `git pull` → **Ctrl+C the personal worker → re-run `worker_personal.py`** (git pull alone does NOT reload code) → confirm `MT5 connected — account=459166`. Then run `/phase1`/`/phase2`/`/changepropfirm` once → personal Trading Fee → ~SGD 0, worker log shows `Fee anchor reset → personal: anchor=…`.
3. `/update layer3` ×2 still pending from sessions 16–17 (`_worker_core.py` + `journaling_worker.py`). No `pyproject.toml` change → no `uv sync`.
4. Post-deploy: `/checksymbols`; close one trade (alert ≤30s, real P&L, no `(est.)`). Start Phase 1: `/phase1` → `4500:1000` → `CONFIRM`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (work on `main`)

## Open items
- **No trade has been entered live yet** — Warren will confirm the layer-2/3 trade behavior (Trade Opened/Closed alerts, geometry) once one goes through. Sanity-check the live alert against `docs/reference/calculations.md` when it does.
- If personal Trading Fee STILL shows −12.40 after a confirmed VPS #2 worker restart → real bug; check for a Windows path/permission issue writing `config/fee_anchor_<login>.json` on VPS #2.
- `docs/Project_Overview.md` + `docs/System_Architecture.md` carry pre-existing uncommitted local edits (not the agent's) — left unstaged; don't ship without Warren's intent. `.obsidian/` + `uv.lock` untracked by design.
- No AGENTS.md adapter exists — invoking `claude-codex-setup` would create one if Codex parity is ever wanted.

## Pick up here
Warren's side: run `/update layer2` and finish the **personal worker restart on VPS #2** (that clears the SGD −12.40 fee). Agent's side: when the first live trade fires, verify the alert math against the KB.
