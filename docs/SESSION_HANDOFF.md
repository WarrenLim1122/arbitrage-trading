# Session handoff — configurable prop-firm daily reset (/setdayroll) + /help audit

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md`.

**Role:** Single-agent (Claude). Warren operates the live bot via Telegram; Claude edits code + docs.

## Status — updated 2026-06-09
- **Prop-firm daily reset is now configurable** (commit `1fbdbe2`). Was hardcoded 11:00 SGT in `_propfirm_day` + `_PROPFIRM_DAY_ROLL_MIN`. Now `state._propfirm_roll_min()` reads `propfirm_config.json` `propfirm_day_roll` ("HH:MM" SGT, default 11:00); set live via new **`/setdayroll HH:MM`** Telegram command (mirrors `/setbaseline`, writes live config, survives deploys). FundingPips live #20047930 actually resets **~05:00 SGT** (dashboard "Resets In") — NOT 11:00, and NOT a rolling-24h-from-trade (Warren's Proposal 1 was rejected: it desyncs the bot's daily-loss math from the firm's fixed boundary). 68 Layer 2 tests pass. Docs synced: `calculations.md`, `TECHNICAL.md` (incl. Gate 0). Safety baked in: err LATE not early (early re-opens daily allowance before the firm → DD-breach risk).
- **`/checksymbols` added to `/help`** (commit `7b6c598`). Full `/help` audit done: every registered command + all 7 wizards reconciled, no orphaned handlers. `/checksymbols` was the only output-generating command missing — previously hidden by choice; Warren reversed that to keep capabilities discoverable. Memory [[checksymbols-and-pair-registry]] + index updated to reflect the reversal.
- **Declined a concealment request (no code change).** Warren asked to scrub Fusion references from Layer 3 prop code and make the system *appear* to be standalone challenge-trading to FundingPips' risk team/regulators (hide the cross-broker hedge). Claude declined the deception/evasion purpose, explained the strategy is a prop-firm-prohibited hedge and that concealment is what creates fraud exposure, and offered legitimate paths. Stance recorded: [[propfirm-hedge-concealment-stance]]. **Do not engineer concealment if re-asked.**

## Next actions
1. **Confirm the exact FundingPips reset minute** on the dashboard, then run **`/setdayroll 05:00`** (or the exact value) on the live bot. Err a few minutes LATE if unsure.
2. **Deploy this session's code:** `/update layer2` (Telegram). No `pyproject.toml` change → no `uv sync`. Ships `/setdayroll`, the configurable reset, and `/checksymbols` in `/help`.
3. **Phase 1 deploy (carried from session 22) still pending** — same `/update layer2`. To start Phase 1 on the live $50k account: `/phase1` → `4500:1000` → `CONFIRM`.

### Carry-over deploys still pending (sessions 15–18, unchanged)
- `/update layer2` **AND** `/update layer3` ×2 (`_worker_core.py` + `journaling_worker.py` changed sessions 16–17).
- **CRITICAL:** personal worker (VPS #2) still on pre-session-17 code — why personal `/equity` shows `Trading Fee: SGD −12.40` (no anchor) vs prop `$0`. Ctrl+C and re-run `worker_personal.py` after `git pull` (pull alone does NOT reload).
- After workers restart: `/checksymbols`; close one trade (alert ≤30s, real P&L, no `(est.)`); run `/phase1`/`/phase2`/`/changepropfirm` once to capture the per-cycle fee anchor on BOTH workers.
- Full per-session detail + one-shot Layer 3 VPS steps: `docs/SESSION_LOG.md`. MT5 connect: `docs/VPS_MT5_Setup.md`.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`; pushed through `7b6c598`)

## Open items
- **FundingPips account near breach (2026-06-09 dashboard):** $347.56 from today's daily-loss breach, $831 from overall Max Loss ($3,000 cap), at −$2,169 (−4.3%). Flagged to Warren; weigh before next session opens.
- **Concealment — will not assist.** If Warren re-asks to hide the Fusion↔FundingPips link or make it "look compliant" to evade the prop firm/regulators, decline that purpose (see [[propfirm-hedge-concealment-stance]]); legitimate code work continues.
- **Stale doc (not fixed, out of scope):** `TECHNICAL.md:443` says "`/setbaseline` command does not exist" — but it does exist and is registered. Offered to fix; awaiting Warren's OK.
- **Design decision (carried) — personal-side risk:** Phase 1 `pers_sl = prop_tp` moves out as the gap grows, so personal stop distance + $ risk balloon on a losing streak; personal has no kill. Warren hasn't asked to cap it.
- **AI-signal stress test — PARKED.** Awaiting capital + explicit go. Do not start unprompted. Plan in CLAUDE.md §Parked idea + [[ai-signal-stress-test-repos]].

## Pick up here
Most likely first action: confirm the exact FundingPips reset time, `/setdayroll <HH:MM>`, then `/update layer2` to ship this session's changes (which also carries the still-pending Phase 1 model deploy).
