# Session handoff — Telegram reporting overhaul, baseline/deposit split, close-alert P&L fix

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, constraints, and the session-14 commit list live in CLAUDE.md (auto-loaded). See CLAUDE.md §Current State → "Session 14".

**Role:** Single Claude Code agent on the arbitrage-trading repo. Warren operates the live bot via Telegram + the two Windows VPSes; Claude edits code and pushes to `main` (standing auto-push permission).

## Status — updated 2026-05-29
- All session-14 work is committed and pushed to `main` (HEAD `d42fde8`). Local working tree matches `origin/main` (code files clean; only pre-existing `docs/Project_Overview.md`, `docs/System_Architecture.md`, and untracked `uv.lock` / `Suggest To Delete/logs/` remain dirty — not this session's concern).
- **Nothing is deployed yet.** Live VPSes still run pre-session-14 code.
- Tests: 90 pass (`~/.local/bin/uv run --extra dev pytest`).
- Key correctness fix this session: close-alert now matches realized P&L by exact position ticket (was pairing wrong trade's P&L) — see CLAUDE.md §Current State.

## Next actions
1. **Deploy — this is the only blocking item.** In Telegram: `/update layer2`, then `/update layer3` → `1` (Personal), then `/update layer3` → `2` (Prop). Worker (`_worker_core.py`) changed, so BOTH workers must restart. No `uv sync` (no `pyproject.toml` change).
2. After deploy, verify on phone: `/equity` (Risk baseline / Deposit / Trading Fee rows, currency-correct), `/setbaseline`, `/setdeposit`, and the next Position Closed alert (Trade P&L / Exit / Trading Fee must match the shown ticket, or show `(est.)`).
3. Warren may want to set real values: `/setbaseline 5000` (prop risk anchor), `/setdeposit personal 486.88`, `/setdeposit prop 5000`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`)

## Open items
- **AGENTS.md does not exist** — no Codex adapter. Invoke `claude-codex-setup` if Codex parity is wanted (not requested).
- Lower-priority queued (from CLAUDE.md): folder reorganization (deletion table at git `accd561`); optional written message-structure spec in TECHNICAL.md (the format is already de-facto standard in code).
- The trading-fee number requires the worker's deal-history read — there is no zero-query way to isolate a real fee. Made it on-demand-only. Warren accepted this; if he later wants plain net P&L with no fee line, that's a known alternative.

## Pick up here
Tell Warren to run `/update layer2` + `/update layer3` ×2, then confirm `/equity` and the next close alert render correct currency-correct numbers. Everything else for session 14 is shipped.
