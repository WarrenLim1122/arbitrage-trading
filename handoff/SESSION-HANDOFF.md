# Session handoff — Telegram reporting overhaul, baseline/deposit split, close-alert + trading-fee fixes

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, constraints, and the full session-14 commit list live in CLAUDE.md (auto-loaded). See CLAUDE.md §Current State → "Session 14".

**Role:** Single Claude Code agent on the arbitrage-trading repo. Warren operates the live bot via Telegram + the two Windows VPSes; Claude edits code and pushes to `main` (standing auto-push permission).

## Status — updated 2026-05-29
- All session-14 work committed + pushed to `main`, HEAD **`033b97e`**. Local tree matches `origin/main` (only pre-existing `docs/*` + untracked `uv.lock` / `Suggest To Delete/logs/` dirty — not this session's concern).
- **Trading Fee is verified correct live** (personal −SGD 6.01, prop −$8.98) after the Layer 3 worker was actually restarted. Formula = `balance − Σ(every deal.profit)`.
- The `/feedebug` diagnostic that was used to find the bug has been **removed** (`033b97e`) at Warren's request.
- Tests: 90 pass.
- Warren said he will run `/update` on all layers after this and close the session.

## Next actions
1. **Deploy HEAD `033b97e` to both layers** (Warren is doing this): `/update layer2`, then `/update layer3` → `1` (Personal) and again → `2` (Prop). **Layer 3 workers must be Ctrl+C'd and re-run — `git pull` alone does NOT reload worker code** (this was the trading-fee bug; see CLAUDE.md §Current State warning + memory mt5-python-integration-constraints #7).
2. After deploy, `/equity` should show the correct Trading Fee on both sides.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`)

## Open items
- **AGENTS.md does not exist** — no Codex adapter. Run `claude-codex-setup` if Codex parity is wanted (not requested).
- Minor unresolved accounting nuance: in `/feedebug` the `comm+swap` check and `bal−dep−gross` check diverged slightly when positions were OPEN (open-position entry commission is charged before any realized profit exists). The displayed fee uses `balance − Σ profit`, which is the canonical all-in figure; the divergence is cosmetic and only appears mid-trade. Revisit only if Warren wants the fee to exclude still-open positions.
- Lower-priority queued (from CLAUDE.md): folder reorganization (deletion table at git `accd561`); optional message-structure spec in TECHNICAL.md.

## Pick up here
Confirm Warren deployed `033b97e` to all layers (Layer 3 via real Ctrl+C restart, not just git pull) and that `/equity` shows correct Trading Fee. Then session 14 is fully closed.
