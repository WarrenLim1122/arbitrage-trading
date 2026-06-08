# Session handoff — CLAUDE.md slimmed to a router + AI-signal research parked

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md` (Session 22 at top).

**Role:** Single-agent (Claude). Warren operates the live bot via Telegram; Claude edits code + docs.

## Status — updated 2026-06-08
- **CLAUDE.md slimmed 333 → 96 lines** (commit `0cea4f7`) into a lean router per the harness doctrine. Nothing deleted — relocated: sessions 14–22 changelog → `docs/SESSION_LOG.md`; VPS MT5 connect how-to → `docs/VPS_MT5_Setup.md`; still-pending carry-over deploys → this file (below). Only Warren's four files were committed; his pre-existing unstaged edits (Project_Overview.md, System_Architecture.md, .obsidian/, the pine, uv.lock) were left untouched.
- **AI-signal research done (no code).** Identified the repos Warren found: `virattt/ai-hedge-fund` (already cloned) + `TauricResearch/TradingAgents` (cloned this session). Both now at `~/.agents/external/`. Honest assessment given: both are educational signal-generators with **no verified live edge**; README stats are overfit backtests; hedging doesn't manufacture edge. Memory: [[ai-signal-stress-test-repos]].
- **`CLAUDE.md` §Parked idea added** — the future "stress-test LLM signals through this harness in demo before capital" plan, flagged DEFERRED / do-not-auto-start. (Not committed yet — see Next actions.)
- **Phase 1 model (session 22) is still the live in-flight code change** and still pending deploy — unchanged this session. Fixed-lot / moving-TP, committed `5f719fe` + `b0a98c5`, 114 tests pass. Details: `docs/SESSION_LOG.md` → Session 22 + `docs/reference/calculations.md`.

## Next actions
1. **Commit the §Parked idea + memory edits** (this session left CLAUDE.md §Parked idea, the new memory, and this handoff uncommitted): `git add CLAUDE.md docs/SESSION_HANDOFF.md && git commit && git push` per the auto-push rule.
2. **Deploy (carried from session 22):** `/update layer2` (Telegram). No `pyproject.toml` change → no `uv sync`.
3. To start Phase 1 on the live $50k account: `/phase1` → `4500:1000` → `CONFIRM`.

### Carry-over deploys still pending (sessions 15–18)
- `/update layer2` (Telegram changes — incl. session-18 `/phase1` fee-anchor reset) **AND** `/update layer3` ×2 (`_worker_core.py` + `journaling_worker.py` changed across sessions 16–17). No `pyproject.toml` change → no `uv sync`.
- **CRITICAL:** the personal worker (VPS #2) is still on pre-session-17 code — that's why personal `/equity` shows `Trading Fee: SGD −12.40` (full since-open residual, no anchor) while prop shows `$0`. Ctrl+C and re-run `worker_personal.py` after `git pull` (git pull alone does NOT reload).
- After workers restart: `/checksymbols`; close one trade (alert ≤30s, real P&L, no `(est.)`); run `/phase1`/`/phase2`/`/changepropfirm` once so the per-cycle fee anchor is captured on BOTH workers.
- Full per-session detail + the one-shot Layer 3 VPS deploy steps: `docs/SESSION_LOG.md`. MT5 connect workflow: `docs/VPS_MT5_Setup.md`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`; pushed through `0cea4f7`; §Parked-idea + memory edits not yet committed)

## Open items
- **AI-signal stress test — PARKED.** Awaiting spare capital + Warren's explicit go. Do NOT start wiring unprompted; surface once if relevant, then wait. Plan in CLAUDE.md §Parked idea + [[ai-signal-stress-test-repos]].
- **Design decision (carried, not a bug) — personal-side risk:** Phase 1 `pers_sl = prop_tp` moves out as the gap grows, so personal stop distance + $ risk balloon on a losing streak and personal has no kill switch. Warren hasn't asked to cap it; offered, awaiting his call.
- **Phase 2 personal ratio still 0.70** (only Phase 1 is ÷5 / 0.20). Left unless Warren says otherwise.

## Pick up here
Most likely first action: commit the uncommitted §Parked-idea/memory/handoff edits, then `/update layer2` to ship the Phase 1 model. The AI-signal idea is parked — do not begin it unless Warren explicitly asks.
