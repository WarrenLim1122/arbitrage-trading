# Session handoff — planning a standalone personal-leg rebuild

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md`.

**Role:** Single-agent. Warren intends to **drop this 4-layer cross-hedge project** and have a
*future* Claude build a **standalone personal-account leg** (personal trades alone, no prop hedge)
from a written plan + build-prompt. This session was the scoping pass. See memory
[[personal-leg-standalone-rebuild]].

## Status — updated 2026-06-14
- **Nothing built or changed.** No code edits, no commits, no `personal-rebuild/` folder yet.
  Warren parked the task: he wants to **supply more strategy context next session before any plan
  is written**, then hand the plan to a separate Claude to build.
- **Analysis done this session (the value to carry forward):** the personal leg today has **no
  independent existence** — it's parasitic on the prop math. Verified against
  `docs/reference/calculations.md` + `layer2/phase1_strategy.py` + `layer2/phase2_strategy.py`:
  - **Sizing:** `pers_lots = round(prop_lots × phase_mult, 2)` (0.20 P1 / 0.70 P2);
    `prop_lots` come from `baseline_equity × 0.67%` over the **prop's** stop. No prop ⇒ **no sizing
    anchor for personal.**
  - **Phase 1 SL:** `pers_sl = prop_tp` — a *derived* level that moves with the prop stage ladder
    and live prop equity (`phase1_strategy.py:171`). No prop ⇒ **personal has no SL definition at
    all in P1.**
  - **Phase 2:** `pers_sl/pers_tp = signal_sl/signal_tp` (raw signal), but lots still come from prop.
  - Conclusion: a rebuild is a **genuine re-design of sizing + geometry**, not "delete prop code."
- **Reusable as-is for the rebuild:** Layer 0 Pine signal (frozen, emits the 14-field webhook —
  near TP ~1000t, far SL ~3700t), the MT5 self-launch connect rule ([[mt5-python-integration-constraints]]),
  `layer3/symbol_mapper.py`, the journaling pipeline (`layer3/journal/`), and the
  webhook→ZMQ→MT5 transport pattern.

## Next actions
1. **Wait for Warren's strategy context** for the standalone personal system, then resolve the 4
   open design forks below (he rejected answering them mid-session — collect them fresh with his new
   context). Only after that, write the plan + build-prompt into a new `personal-rebuild/` folder.

## The 4 open design forks (resolve BEFORE writing the plan)
1. **Sizing anchor** — % of live personal equity per trade (recommended; auto-scales) · % of a
   fixed baseline (stable $/trade, set via Telegram) · flat fixed-$ per trade.
   All: `lots = risk_$ / (SL_distance × k)`, `k` from `dollar_per_unit` (`strategy_common.py:13`).
2. **SL/TP geometry** — raw signal SL+TP (RR ≈ 0.27, near-TP/far-SL → needs high win-rate) ·
   signal SL + fixed target-RR TP · signal TP + tightened SL to a target RR. (Phase 1's old
   reward-targeting scheme can't be reused — it depended on prop equity/stages.)
3. **Risk halts** — personal has **none** today. Options: daily + overall DD halt (mirror prop
   K1/K2) · daily-loss-only · none.
4. **Architecture** — clean greenfield 2-service (Receiver on Linux: webhook + news/time filter +
   sizing + Telegram + ZMQ push; Worker on Windows: MT5 execute + position watch + journal) vs
   strip the existing 4-layer repo. (Min 2 processes regardless: public HTTPS receiver + Windows MT5
   worker.)

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`)

## Open items
- Warren to provide his intended **standalone personal strategy** (how he wants to size, where SL/TP
  come from, whether he wants any equity-based halts) — the plan blocks on this.
- Untracked in working tree (pre-existing, not from this session): `.obsidian/`, a stray
  `layer0/Flipped RSI Divergence Indicator.pine`, `logs/demo_chart_*.png`, `uv.lock`.

## Pick up here
Ask Warren for his standalone-personal strategy details, walk the 4 forks above with his answers,
then scaffold `personal-rebuild/` with the design doc + a self-contained build-prompt for a fresh
Claude. Do NOT start building the system itself — only the plan + prompt.
