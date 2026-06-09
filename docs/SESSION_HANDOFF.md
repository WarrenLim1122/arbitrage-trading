# Session handoff — Layer 2 deployed, /help restructured, deploy footgun + auto-commit governance

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md`.

**Role:** Single-agent (Claude). Warren operates the live bot via Telegram; Claude edits code + docs.

## Status — updated 2026-06-09
- **`/help` menu restructured** (commit `e7689ae`, NOT yet deployed). One rule now: reads stay in their domain section; every parameter-setter lives in **Configuration**. Moved `/setbaseline` `/setdayroll` `/setdeposit` `/setmaxpos` out of Positions & Risk → Configuration (joining `/setwindow` `/changepropfirm`); new read-only **Account & Symbols** section (`/propfirm` `/checkaccount` `/checksymbols`); `/consistency` moved into Positions & Risk. Pure `/help` text edit in `telegram_handlers.py:2576+`, no logic change. Known trade-off accepted: `/maxpos` (view) and `/setmaxpos` (set) now sit in different sections.
- **Layer 2 is LIVE on `43c4330`** (deployed earlier this session) — shipped configurable prop reset + `/setdayroll`, `/checksymbols` in `/help`, and the Phase 1 fixed-lot/moving-TP model. `e7689ae` (the `/help` restructure) is the only commit ahead of the running VPS code.
- **Recurring `/update layer2` deploy failure diagnosed.** Tracked `config/*.json` are runtime-mutated on the VPS → `git pull` aborts. Unblock = backup→`git stash`→`git pull`→restore live config→`git stash drop`→restart. Captured in CLAUDE.md §Workflow Rules + memory [[deploy-runtime-config-conflict]]. **Permanent fix (untrack runtime configs + `.example` seeding) offered but NOT done — awaiting Warren's go.**
- **New standing rule, all projects: auto-commit AND push after every edit, never ask** (revert is the safety net). In global `~/.claude/CLAUDE.md` §Git Workflow + per-project CLAUDE.md. Memory [[always-commit-and-push]].

## Next actions
1. **`/update layer2`** to ship the `/help` restructure (`e7689ae`). If the pull aborts on `config/*.json`, use the unblock sequence in [[deploy-runtime-config-conflict]]. No `pyproject.toml` change → no `uv sync`.
2. **`/setdayroll 05:00`** on the live bot if not already done — restored config defaults to 11:00; FundingPips #20047930 resets ~05:00 SGT. Confirm exact minute; err LATE if unsure.
3. **Decide on the permanent deploy-footgun fix** (untrack runtime `config/*.json` + `.example` seeding). See [[deploy-runtime-config-conflict]].
4. **Layer 3 workers still on old code (carry-over, sessions 15–18).** `/update layer3` ×2 + Ctrl+C/re-run both workers (`git pull` alone does NOT reload). CRITICAL: personal worker (VPS #2) pre-session-17 → personal `/equity` shows `Trading Fee: SGD −12.40` (no anchor). After restart: `/checksymbols`; close one trade (real P&L, no `(est.)`); run `/phase1`/`/phase2`/`/changepropfirm` once to capture per-cycle fee anchor on BOTH workers. Steps: `docs/SESSION_LOG.md`, `docs/VPS_MT5_Setup.md`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`; pushed through `e7689ae`)

## Open items
- **FundingPips account near breach (2026-06-09 dashboard):** $347.56 from today's daily-loss breach, $831 from overall Max Loss ($3,000 cap), at −$2,169 (−4.3%). Weigh before next session opens.
- **Concealment — will not assist.** If Warren re-asks to hide the Fusion↔FundingPips link or make it "look compliant" to evade the prop firm/regulators, decline that purpose ([[propfirm-hedge-concealment-stance]]); legitimate code work continues.
- **Stale doc (not fixed, out of scope):** `TECHNICAL.md:443` says "`/setbaseline` command does not exist" — but it does. Offered to fix; awaiting Warren's OK.
- **Design decision (carried) — personal-side risk:** Phase 1 `pers_sl = prop_tp` moves out as the gap grows, so personal stop distance + $ risk balloon on a losing streak; personal has no kill. Warren hasn't asked to cap it.
- **AI-signal stress test — PARKED.** Awaiting capital + explicit go. Do not start unprompted. Plan in CLAUDE.md §Parked idea + [[ai-signal-stress-test-repos]].

## Pick up here
Most likely first action: `/update layer2` to ship the `/help` restructure (`e7689ae`), using the deploy-footgun unblock sequence if the pull aborts on `config/*.json`.
