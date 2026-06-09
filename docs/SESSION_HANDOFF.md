# Session handoff — Layer 2 deployed + auto-commit governance + deploy footgun diagnosed

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md`.

**Role:** Single-agent (Claude). Warren operates the live bot via Telegram; Claude edits code + docs.

## Status — updated 2026-06-09
- **Layer 2 is LIVE on `43c4330`.** This session's `/update layer2` succeeded on VPS #1. The pull `f11c8fd..43c4330` (12 files) shipped: the configurable prop-firm reset + `/setdayroll`, `/checksymbols` in `/help`, AND the Phase 1 fixed-lot/moving-TP model that had been pending since session 22. `layer2.service` confirmed `active (running)`, clean startup, no Python errors.
- **Diagnosed the recurring `/update layer2` deploy failure.** Root cause: `config/propfirm_config.json` is tracked in git but runtime-mutated on the VPS, so `git pull` aborted ("local changes would be overwritten"). It was NEVER about uncommitted local edits. Unblocked with backup→`git stash`→`git pull`→restore live config→`git stash drop`→restart. Captured in CLAUDE.md §Workflow Rules + memory [[deploy-runtime-config-conflict]]. **Proper fix (untrack runtime configs + `.example` seeding) is offered but NOT done — awaiting Warren's go.**
- **Committed the doc edits that were sitting uncommitted** (`43c4330`: Project_Overview.md + System_Architecture.md) — these were the working-tree changes Warren saw, unrelated to the deploy error.
- **New standing rule across ALL projects: auto-commit AND push after every edit, never ask** (revert is the safety net). Codified in global `~/.claude/CLAUDE.md` §Git Workflow + per-project CLAUDE.md (dating-app `ae1740f` pushed; second-brain `e8d24e7` local-only; company-system/app; CFI Testing conditional). Memory [[always-commit-and-push]].

## Next actions
1. **`/setdayroll 05:00`** on the live bot — the restored config lacks `propfirm_day_roll`, so it's defaulting to 11:00 SGT. FundingPips live #20047930 resets ~05:00 SGT. Confirm exact minute on dashboard; err LATE if unsure.
2. **Decide on the proper deploy-footgun fix** (untrack runtime `config/*.json` + `.example` seeding). If yes → plan + implement; this permanently ends the pull-abort. See [[deploy-runtime-config-conflict]].
3. **Layer 3 workers still on old code (carry-over, sessions 15–18).** `/update layer3` ×2 + Ctrl+C/re-run both workers (`git pull` alone does NOT reload). CRITICAL: personal worker (VPS #2) pre-session-17 → personal `/equity` shows `Trading Fee: SGD −12.40` (no anchor). After restart: `/checksymbols`; close one trade (real P&L, no `(est.)`); run `/phase1`/`/phase2`/`/changepropfirm` once to capture per-cycle fee anchor on BOTH workers. Steps: `docs/SESSION_LOG.md`, `docs/VPS_MT5_Setup.md`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`; pushed through `43c4330`)

## Open items
- **FundingPips account near breach (2026-06-09 dashboard):** $347.56 from today's daily-loss breach, $831 from overall Max Loss ($3,000 cap), at −$2,169 (−4.3%). Weigh before next session opens.
- **Concealment — will not assist.** If Warren re-asks to hide the Fusion↔FundingPips link or make it "look compliant" to evade the prop firm/regulators, decline that purpose ([[propfirm-hedge-concealment-stance]]); legitimate code work continues.
- **Stale doc (not fixed, out of scope):** `TECHNICAL.md:443` says "`/setbaseline` command does not exist" — but it does. Offered to fix; awaiting Warren's OK.
- **Design decision (carried) — personal-side risk:** Phase 1 `pers_sl = prop_tp` moves out as the gap grows, so personal stop distance + $ risk balloon on a losing streak; personal has no kill. Warren hasn't asked to cap it.
- **AI-signal stress test — PARKED.** Awaiting capital + explicit go. Do not start unprompted. Plan in CLAUDE.md §Parked idea + [[ai-signal-stress-test-repos]].

## Pick up here
Most likely first action: `/setdayroll 05:00` on the live bot to set the now-defaulted reset time. Then decide whether to implement the permanent deploy-footgun fix, and tackle the still-pending Layer 3 worker restart (personal `/equity` fee anchor).
