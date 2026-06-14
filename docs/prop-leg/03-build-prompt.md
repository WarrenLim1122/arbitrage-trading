# 03 — Build Prompt (the one-paste kickoff)

## How Warren uses this
1. Open a Claude Code session **inside this repo** (`arbitrage-trading`) — the agent reads the kit here
   and builds in a **separate sibling folder**. It never edits this repo.
2. Tell the agent: **"Read `docs/prop-leg/03-build-prompt.md` and follow it."** (Or paste the box below.)

---

```
You are an autonomous build agent. You are running INSIDE the `arbitrage-trading` repo, which is your
read-only REFERENCE. Build a NEW, standalone prop-firm-challenge trading system in a SEPARATE new folder
(its own git repo) at:

    ~/Coding Projects/prop-leg-system/      (confirm the name with Warren at CP-0)

Your complete, non-negotiable specification is the kit at:

    docs/prop-leg/        (relative to this arbitrage-trading repo you are in)

🔒 NAMING RULE (read 00 — applies everywhere): this is a self-contained system that trades ON ITS OWN.
In ALL code, comments, Pine, config, logs, and Telegram messages you NEVER reference another account or
system and NEVER use the words personal / inverse / mirror / hedge / flip / opposite. Describe every
behavior in this system's own absolute terms (e.g. a SHORT with stop above entry and far target below =
a "breakout-fade" setup).

START by reading IN FULL, in order (do not scan the rest of the tree):
    docs/prop-leg/00-AGENT-START-HERE.md      <- rules + naming rule + checkpoint protocol; read first
    docs/prop-leg/01-master-plan.md
    docs/prop-leg/02-calculation-spec.md       <- exact geometry (P1+P2) + kills K1–K5; memorize
    docs/prop-leg/04-system-architecture.md
    docs/prop-leg/05-data-contracts.md
    docs/prop-leg/06-build-tasks.md            <- YOUR RUNBOOK: execute T0..T14 in order
    docs/prop-leg/07-telegram-spec.md
    docs/prop-leg/08-test-plan.md
    docs/prop-leg/09-deploy-runbook.md
    docs/prop-leg/10-signal-engine-pine.md

THEN execute 06-build-tasks.md from T0 to T14, top to bottom. Rules (full list in 00):
  - You READ arbitrage-trading; you WRITE only to ~/Coding Projects/prop-leg-system/. NEVER edit, delete,
    or commit anything in arbitrage-trading. The original system logic is FROZEN.
  - TDD: write each task's tests (08) FIRST, watch fail, implement to green.
  - The math is fixed — geometry + kills must match 02-calculation-spec.md exactly; tests pin the numbers.
  - When porting from the reference, STRIP every second-leg reference (drop pers_* outputs, the phase
    multiplier, the dual-leg pre-flight). This system is single-account.
  - Commit + push the NEW repo after every task. Stop ONLY at CP-0..CP-3; otherwise run continuously.
  - If genuinely ambiguous, mirror the reference's prop-side behavior, note it in the commit, continue.

Begin at T0: stop at CP-0 to confirm the new-repo path with Warren, then proceed.
```

---

## What "done" looks like
- **T0–T11 (autonomous):** full code + green `pytest` + a correct dry-run trace + the kills-fire
  simulation (`08 §9`), all in the new sibling repo.
- **CP-1:** agent hands back for Warren's baseline/challenge numbers + credentials + host.
- **T13–T14:** demo deploy + ≥7-day soak, then go-live decision.

If the agent ever feels lost: `00` (rules) → `06` (next task) → `04`/`05` (structure/contracts) →
`02` (math) → `10` (signal).
