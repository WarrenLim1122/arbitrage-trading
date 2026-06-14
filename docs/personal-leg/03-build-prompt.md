# 03 — Build Prompt (the one-paste kickoff)

## How Warren uses this next session
1. Open a Claude Code session **inside this repo** (`arbitrage-trading`) — the agent lives here, reads
   the whole kit directly, and builds in a **separate sibling folder**. It never edits this repo.
2. Just tell the agent: **"Read `docs/personal-leg/03-build-prompt.md` and follow it."**
   (Or paste the box below — same effect.)

That's it. The agent reads the kit, then at its first checkpoint creates the new project folder at
`~/Coding Projects/personal-leg-system/`, `git init`s it, and builds everything there from T0→T14,
stopping only at the labelled checkpoints.

---

```
You are an autonomous build agent. You are running INSIDE the `arbitrage-trading` repo, which is your
read-only REFERENCE. You will BUILD a new standalone single-leg personal trading system in a SEPARATE
new folder — NOT inside this repo. Your complete, non-negotiable specification is the kit at:

    docs/personal-leg/         (relative to this arbitrage-trading repo you are in)

The new system you build lives at a SIBLING folder, its own git repo:

    ~/Coding Projects/personal-leg-system/      (confirm the name with Warren at CP-0)

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
  - You READ from this arbitrage-trading repo; you WRITE only to ~/Coding Projects/personal-leg-system/.
    NEVER edit, delete, move, or commit anything inside arbitrage-trading.
  - TDD: write each task's tests (08-test-plan.md) FIRST, watch fail, implement to green.
  - The math is fixed — compute_personal_geometry must match 02-calculation-parity.md exactly; the
    first geometry test pins the numbers. Do not "improve" it.
  - Commit + push the NEW repo after every task (each task gives a commit message). Small, frequent commits.
  - Stop ONLY at the labelled CHECKPOINTS (CP-0, CP-1, CP-2, CP-3). Everything else runs autonomously,
    start to finish. Don't pause for routine coding decisions — the spec already made them. If something
    is genuinely ambiguous, mirror the reference system's behavior, note it in the commit, and continue.

Begin at T0: stop at CP-0 to confirm the new-repo path with Warren (suggested default
`~/Coding Projects/personal-leg-system`), then proceed.
```

---

## What "done" looks like
- **T0–T11 (autonomous):** full code + green `pytest` suite + a correct end-to-end dry-run trace, all in
  the new sibling repo.
- **CP-1:** agent hands back for Warren's numbers/credentials/host.
- **T13–T14:** demo deploy + ≥7-day soak, then Warren's go-live decision.

The kit is deliberately rigid so the agent always knows the whole structure and never has to invent a
contract mid-build. If it ever feels lost: `00` (rules) → `06` (what's next) → `04`/`05` (structure/contracts).

## Verified working setup
- The agent session runs **inside `arbitrage-trading`** → it can read `docs/personal-leg/` and every
  referenced source file directly (exact paths in `00`).
- `~/Coding Projects/` is writable, so the agent can create the sibling `personal-leg-system/` folder
  (this repo blocks new top-level dirs *within itself*, but the new build is a separate folder, unaffected).
