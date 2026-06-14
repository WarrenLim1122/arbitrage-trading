# 03 — Build Prompt (the one-paste kickoff)

## How Warren uses this next session
1. Open a Claude Code session **inside this repo** (`arbitrage-trading`) — the agent reads the kit here
   and builds in a **separate sibling folder**. It never edits this repo.
2. Tell the agent: **"Read `docs/personal-leg/03-build-prompt.md` and follow it."** (Or paste the box below.)

The agent reads the kit, then at CP-0 confirms the MTProto feasibility gate + path, creates
`~/Coding Projects/personal-leg-system/`, `git init`s it, and builds T0→T13, stopping only at checkpoints.

---

```
You are an autonomous build agent. You are running INSIDE the `arbitrage-trading` repo, which is your
read-only REFERENCE. Build the personal trading system in a SEPARATE new folder (its own git repo):

    ~/Coding Projects/personal-leg-system/      (confirm the name with Warren at CP-0)

Your complete, non-negotiable specification is the kit at:  docs/personal-leg/

WHAT THIS SYSTEM IS (read 00 + 01 carefully): the personal system has NO signal of its own. It is the
INVERSE FOLLOWER of a separate prop system. It reads the prop bot's Telegram alerts (Trade Opened /
Position Closed / Kill K1–K5) via a Telegram USER client (MTProto/Telethon — a Bot API bot CANNOT read
another bot's messages) and mirrors each event on the personal MT5 account as the exact inverse hedge:
pers_dir = inverse(prop_dir), pers_lots = round(prop_lots × phase_multiplier, 2), pers_sl = prop_tp,
pers_tp = prop_sl. This reproduces the original coupled hedge while the prop system stays unaware.

START by reading IN FULL, in order (do not scan the rest of the tree):
    docs/personal-leg/00-AGENT-START-HERE.md      <- rules + MTProto feasibility gate + checkpoints; first
    docs/personal-leg/01-master-plan.md
    docs/personal-leg/02-calculation-parity.md     <- the reconstruction math; memorize it
    docs/personal-leg/04-system-architecture.md
    docs/personal-leg/05-data-contracts.md
    docs/personal-leg/06-build-tasks.md            <- YOUR RUNBOOK: execute T0..T13 in order
    docs/personal-leg/07-telegram-spec.md
    docs/personal-leg/08-test-plan.md
    docs/personal-leg/09-deploy-runbook.md
    docs/personal-leg/10-prop-follower.md          <- the core: read + route the prop bot's alerts

THEN execute 06-build-tasks.md from T0 to T13, top to bottom. Rules (full list in 00):
  - You READ arbitrage-trading; you WRITE only to ~/Coding Projects/personal-leg-system/. NEVER edit,
    delete, move, or commit anything in arbitrage-trading.
  - TDD: write each task's tests (08) FIRST, watch fail, implement to green.
  - The math is fixed — reconstruct_personal must match 02 exactly; the first test pins the numbers.
  - Personal has no webhook/Pine/news-filter — its input is the prop's Telegram alerts (10).
  - Commit + push the NEW repo after every task. Stop ONLY at CP-0..CP-3; otherwise run continuously.
  - If genuinely ambiguous, mirror the reference's pers_* behavior, note it in the commit, continue.

Begin at T0: at CP-0, FIRST confirm with Warren that he will run an MTProto user session (the blocking
feasibility gate) and get api_id/api_hash/account + group id + prop bot id; then confirm the repo path.
If he won't run a user session, STOP and report — the model isn't buildable without it.
```

---

## What "done" looks like
- **T0–T10 (autonomous):** full code + green `pytest` + a correct dry-run trace (prop event → hedge), in
  the new sibling repo.
- **CP-1:** agent hands back for Warren's `phase_multipliers`, the prop parse contract, credentials, host.
- **T12–T13:** demo deploy (needs the prop demo running too) + ≥7-day soak, then go-live decision.

If the agent feels lost: `00` (rules) → `06` (next task) → `10` (prop-follow) → `04`/`05`
(structure/contracts) → `02` (math).

## Verified setup
- The session runs **inside `arbitrage-trading`** → it reads `docs/personal-leg/` + referenced sources.
- `~/Coding Projects/` is writable → the sibling repo creates fine.
- **Feasibility note:** the prop-alert reader needs a Telegram **user session** (MTProto), not a bot —
  this is the one external dependency Warren must approve at CP-0.
