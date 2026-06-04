# Session handoff — knowledge base built + folder reorg done

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single Claude agent + Warren (operator). Warren runs all Telegram `/update` deploys and VPS actions himself; agent edits code on `main` and pushes.

## Status — updated 2026-06-04 (session 18)

- **Knowledge base BUILT** — `docs/reference/` (start at `index.md`): `architecture.md`,
  `calculations.md`, `messages.md`, `execution.md`, `deployment.md`. Written from a code-verified
  file-by-file pass (layers 0–3 + config + tests). This is the persistent "brain" Warren asked for.
  **From now on: consult the KB first to locate file:line, then act; keep it in sync on every code
  change** (CLAUDE.md now leads with a "Knowledge base — CONSULT FIRST" block). Memory:
  [[knowledge-base-workflow]].
- **Folder reorganization DONE** — the accd561 deletion table is fully cleared (superpowers/,
  AI_Workflow.md, backfill_journal.py, TEST-ONLY pine, skill-creator were already gone; scripts
  already split into `dev-tests/` + `vps-setup/`). This session: removed empty `*.log`; fixed
  `docs/README.md` (dead AI_Workflow link → KB pointer). Residue: a root `.DS_Store` (gitignored,
  env-locked, harmless).
- **Green (2) — message-structure spec in TECHNICAL.md — intentionally SKIPPED** per Warren. The
  format is already documented in `docs/reference/messages.md`.
- No code/behavior changes this session — docs only. Tests unaffected (107 pass as of session 17).

## Next actions
1. **Carry-over deploy (Warren does this) — sessions 15–17, still pending:** `/update layer2`
   (Telegram) + `/update layer3` ×2 (`_worker_core.py` + `journaling_worker.py` changed across
   16–17). No `pyproject.toml` change → no `uv sync`. After workers restart: `/checksymbols`; close
   one trade (close alert ≤30 s, real net P&L, no `(est.)`); run `/changepropfirm` or `/phase2` once
   to capture the per-cycle fee anchor (else prop `/equity` shows the bogus `$+50,000`). To start
   Phase 1 on the live $50k account: `/phase1` → `4500:1000` → `CONFIRM`.
2. **No queued build task remains.** Work the to-do list one item at a time, only when Warren asks.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (work on `main`)

## Open items
- `docs/Project_Overview.md` + `docs/System_Architecture.md` carry pre-existing uncommitted local
  edits (present before this session, not the agent's). Left unstaged — do not ship without Warren's
  intent. `.obsidian/` and `uv.lock` are also untracked by design (uv.lock stays untracked).
- Account switch on VPS #2 (personal): confirm which login the worker bound to via the
  `MT5 connected — account=…` line / `/health` after deploy.

## Pick up here
The KB is the persistent brain. For any change request: open `docs/reference/index.md`, jump to the
right page, then edit the code it points to — and update that page in the same session.
