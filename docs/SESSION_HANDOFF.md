# Session handoff — planning a standalone personal-leg rebuild

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md`.

**Role:** Single-agent. Warren intends to **drop this 4-layer cross-hedge project** and have a
*future* Claude build a **standalone personal-account leg** (personal trades alone, no prop hedge)
from a written plan + build-prompt. This session was the scoping pass. See memory
[[personal-leg-standalone-rebuild]].

## Status — updated 2026-06-14 (plan WRITTEN)
- **Master plan now written** at `docs/personal-leg/` (README + 01-master-plan + 02-calculation-parity
  + 03-build-prompt). NOT built — plan + build-prompt only, by Warren's choice. Couldn't use a repo-root
  folder (EPERM on new top-level dirs); lives under `docs/`. Also this session: Phase 2 `prop_risk_pct`
  0.67%→1.0%.
- **Warren's strategy context (resolved the 4 forks):** (1) sizing = % of a fixed personal baseline,
  Telegram-set; (2) phases dropped → two-mode toggle differing by **risk % only**, same geometry;
  (3) geometry = raw signal SL/TP = **the prop's calc logic applied in the reverse (personal) direction**
  — size native `risk_$ = personal_baseline × risk_pct` over the personal leg's OWN stop `|entry−signal_sl|`,
  kernel `dollar_per_unit` unchanged; (4) daily + overall DD halt on personal equity (mirror K1/K2);
  (5) clean greenfield 2-service (Linux Receiver + Windows Worker), reuse Layer 0 Pine/symbol_mapper/
  journaling/MT5 self-launch/transport.
- **Still to confirm (in `01-master-plan.md §9`, non-blocking):** the two mode %s (default 1%/2%),
  daily/overall DD % (suggested 4%/8%), `personal_baseline` value, Receiver host.
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
1. **Forks resolved + plan written** — see `docs/personal-leg/`. Next: Warren confirms the open numbers
   in `01-master-plan.md §9`, then a **separate** Claude session runs `docs/personal-leg/03-build-prompt.md`
   to build it (TDD, demo-first). Do NOT start building from a planning session.

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
Plan is at `docs/personal-leg/`. If Warren returns the §9 numbers, fold them into `01-master-plan.md`
and `personal_config.json` defaults. When he's ready to build, open a fresh session and paste
`docs/personal-leg/03-build-prompt.md`. Do NOT build from a planning session.
