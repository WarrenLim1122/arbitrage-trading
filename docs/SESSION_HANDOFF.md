# Session handoff — Phase 1 rewritten to fixed-lot / moving-TP (+ consistency pass + 2 bug fixes)

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail: CLAUDE.md §Current State → Session 22.

**Role:** Single-agent (Claude). Warren operates the live bot via Telegram; Claude edits code + docs.

## Status — updated 2026-06-07
- **Phase 1 geometry replaced** (`layer2/phase1_strategy.compute_geometry`) with the FIXED-LOT / moving-TP model Warren specified over a long back-and-forth this session. Final, verified, committed `5f719fe` + `b0a98c5`. The exact rule (memorise it — it took many iterations to land):
  - Signal is for PERSONAL; prop inverts. **Only signal TP + entry are used; signal SL is DISCARDED.**
  - `prop_sl = signal_tp` (near). `lots_prop = fixed_risk / (|signal_tp−entry| × k)` → **lots FIXED** (gold $1k→1.0, $2k@$100k→2.0). `lots_pers = lots_prop × 0.20`.
  - `prop_tp` = **calculated**: `reward_gap / (lots_prop × k)`; it carries the stage gap and **becomes `pers_sl`**. `pers_tp = prop_sl = signal_tp`.
  - RR = `reward_gap / fixed_risk` → 4.5/5.5/6.5 (each loss +$1000 gap), ~0.25 after a stage win. Lots never move; the TP does.
- **Phase 2 untouched** (already correct): all signal levels, prop exact inverse, `baseline×0.67%`, lots vary with signal TP distance, RR = signal ratio.
- **2 bug fixes** (`b0a98c5`, found by running multi-trade sims): degenerate prop-TP reject guard; zero-tick 500-crash hardened (`pers_*` fields coalesce 0/None via `or`).
- **Consistency pass** done — no file still describes the old "lots scale, TP fixed" model. Swept docs/TECHNICAL.md/CLAUDE.md/memories; `TECHNICAL.md §Immutable Risk Math` split per phase.
- Tests **114 pass**. A wrong mid-session "unify P1 into P2 box" commit (`993ed31`) was reverted (`3132fb9`) — net zero; don't resurrect it.

## Next actions
1. **Deploy:** `/update layer2` (Telegram). No `pyproject.toml` change → no `uv sync`. (Carry-over Layer 3 deploys from sessions 15–18 still pending separately — see CLAUDE.md top "Still-pending deploy".)
2. To start Phase 1 on the live $50k account: `/phase1` → `4500:1000` → `CONFIRM`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`, pushed through `b0a98c5`)
- Throwaway sims (safe to ignore/delete): `/tmp/sim_full.py`, `/tmp/sim_p2.py`, `/tmp/fixed_signal.py`, `/tmp/dryrun_*.py`

## Open items
- **Design decision, not a bug — personal-side risk:** because `pers_sl = prop_tp` (moves out as the gap grows), the personal stop distance + $ risk balloon on a losing streak (~$900→$1700 in the sim) and **personal has no kill switch**. Warren has NOT asked to cap it; offered, awaiting his call.
- **Phase 2 personal ratio is still 0.70** (only Phase 1 is ÷5 / 0.20). Warren only specified ÷5 for Phase 1; left 0.70 unless he says otherwise.
- No AGENTS.md adapter exists; `claude-codex-setup` would create one if Codex parity is wanted (not requested).

## Pick up here
If Warren confirms the model is live, the next action is almost certainly the personal-risk-cap question (Open items #1) or just deploying via `/update layer2`. The Phase 1 math is final — do not re-derive it; read `docs/reference/calculations.md` §Phase 1 + [[phase1-reward-risk-scaling]].
