# 03 — Build Prompt (the one-paste kickoff)

Open a **fresh Claude Code session** with **read access to the `arbitrage-trading` repo** (the reference)
and paste the box below. It launches the agent into the full kit and the T0→T14 runbook. Do not build
from the planning session.

---

```
You are an autonomous build agent. Build a NEW standalone single-leg personal trading system in a NEW
repository, using the existing `arbitrage-trading` repo ONLY as a read-only reference. Your complete,
non-negotiable specification is the kit at:

    arbitrage-trading/docs/personal-leg/

START by reading these files IN FULL, in this order (do not scan the rest of the tree):
    docs/personal-leg/00-AGENT-START-HERE.md      <- operating rules + checkpoint protocol; read first
    docs/personal-leg/01-master-plan.md
    docs/personal-leg/02-calculation-parity.md     <- the exact math; memorize it
    docs/personal-leg/04-system-architecture.md
    docs/personal-leg/05-data-contracts.md
    docs/personal-leg/06-build-tasks.md            <- YOUR RUNBOOK: execute tasks T0..T14 in order
    docs/personal-leg/07-telegram-spec.md
    docs/personal-leg/08-test-plan.md
    docs/personal-leg/09-deploy-runbook.md

THEN execute 06-build-tasks.md from T0 to T14, top to bottom. Rules (full list in 00):
  - TDD: write each task's tests (08-test-plan.md) FIRST, watch fail, implement to green.
  - The math is fixed — compute_personal_geometry must match 02-calculation-parity.md exactly; the
    first geometry test pins the numbers. Do not "improve" it.
  - Commit + push after every task (each task gives a commit message). Small, frequent commits.
  - Build only in the NEW repo. NEVER edit/delete/move anything in arbitrage-trading.
  - Stop ONLY at the labelled CHECKPOINTS (CP-0, CP-1, CP-2, CP-3). Everything else runs autonomously,
    start to finish. Don't pause for routine coding decisions — the spec already made them. If something
    is genuinely ambiguous, mirror the reference system's behavior, note it in the commit, and continue.

Begin at T0: stop at CP-0 to confirm the target repo path with Warren (suggested default
`~/Coding Projects/personal-leg-system`), then proceed.
```

---

## What "done" looks like
- **T0–T11 (autonomous):** full code + green `pytest` suite + a correct end-to-end dry-run trace.
- **CP-1:** agent hands back for Warren's numbers/credentials/host.
- **T13–T14:** demo deploy + ≥7-day soak, then Warren's go-live decision.

The kit is deliberately rigid so the agent always knows the whole structure and never has to invent a
contract mid-build. If the agent ever feels lost, the answer is in `00` (rules) → `06` (what's next) →
`04`/`05` (structure/contracts).
