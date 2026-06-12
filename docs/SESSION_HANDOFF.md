# Session handoff — journaling re-armed on both workers; new prop account; first entry unverified

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md`.

**Role:** Single-agent (Claude). Warren operates the live bot via Telegram and executes all VPS-side steps by hand (Claude cannot reach the Windows VPSes).

## Status — updated 2026-06-12
- Journaling outage from last session is FIXED end-to-end. Warren's hand-typed `.env` edits had 5 errors (wrong `FIREBASE_PROJECT_ID` = user-id pasted in, stray `s` on `FIREBASE_DATABASE_ID`, empty `FIREBASE_STORAGE_BUCKET`, `SCREENSHOT_STORAGE=local` which the uploader doesn't support, stale `JOURNAL_BROKER=MetaQuotes Demo`). Fixed by committing ready-to-paste blocks `layer3/env_journal_personal.txt` + `layer3/env_journal_prop.txt` (commit `959f79c`) which Warren pulled and pasted into each VPS `.env`. Reuse those files for any future account/VPS rebuild — never let the journal block be hand-typed again.
- Both workers restarted; startup logs verified: "Journal modules started (dry_run=false)" on personal AND prop, sockets bound, retry/pending queues running. Canonical Firebase values verified live from the Mac (Firestore query): project `gen-lang-client-0206326169`, DB `ai-studio-88ba4d0a-7b6e-4d07-a03b-675ed3bc8607`, user `WCzOHPl8C4Q1aa3EDHkOGhdH9To1`, bucket `gen-lang-client-0206326169.firebasestorage.app`.
- Journal tags: personal = `live`/`FusionMarkets`, prop = `prop`/`FundingPips`. `SCREENSHOT_ONLY_FOR_TP_SL=true` — charts attach only on TP/SL closes.
- **Prop account changed (again): now `20116670` on FundingPips-SIM1** (was `20047930`). Terminal + `.env` match, worker boots. New account exposes only **9 of 33** registry symbols (majors + XAU/XAG; even USDSGD/USDSEK missing); personal finds 20.
- Last session's chart-renderer fix (`9d419d8`) is now deployed by these restarts — next journal chart should have correct markers/labels.

## Next actions
1. After the next TP/SL trade close, verify the first post-fix journal entry: query Firestore from the Mac (recipe in memory [[firestore-journal-verification]]) — expect a doc under `users/WCzOHPl8C4Q1aa3EDHkOGhdH9To1/trades` with a populated chart URL, and the entry visible at warrenlimzf.com/journal.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: main, all session work pushed through `959f79c`

## Open items
- Warren was told to run `/checksymbols` to confirm all 7 armed TradingView pairs are FOUND on the new prop account (only 9 symbols exist there) — not yet confirmed done.
- Baseline check after the prop account switch (`/changepropfirm` or `/setbaseline` if the new account's starting equity differs) — flagged to Warren, not confirmed done.
- Pre-existing: deploy runtime-config proper fix (untrack `config/*.json` + `.example` seeding) still PENDING Warren's go ([[deploy-runtime-config-conflict]]).

## Pick up here
Ask Warren whether a trade has closed since 2026-06-12 02:46 SGT; if yes, run the Firestore last-entry query to confirm journaling + chart upload are alive, then check off the `/checksymbols` and baseline open items.
