# Session handoff — self-healing $0 NO_MONEY guard + dual-session theory downgraded

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single-agent (Claude Code). Warren operates the VPSes + Telegram; Claude edits code/docs and pushes to `main`.

## Status — updated 2026-06-04

This session was a diagnosis + one code fix. No live trade was placed.

- **Question answered:** "previous trade not entered — network or me being active on the prop account?"
  - **Not network.** Both terminals stream live (VPS#3 prop `143/1 Kb`, VPS#2 personal `1144/2 Kb`, Market Watch ticking). A network/feed problem produces a `transient` verdict which does NOT block; only a hard `reject` blocks (`logic_core.py:1573`). The failure was a hard NO_MONEY reject, which lag cannot produce.
  - **Dual-session GUI theory (session 19) is now UNPROVEN.** Warren ran two MT5 GUIs open before with trades filling fine, and the session-19 diagnostic log wasn't deployed at the failure → zero evidence. Root cause of the $0 read is genuinely unknown (launch-time race / feed-reconnect blip / login contention all plausible).
- **Code shipped (`main`):** self-healing guard in `_build_order_check_reply` (`layer3/_worker_core.py`). On a NO_MONEY reject whose `order_check margin_free == 0.0` exactly (degenerate signature — a real shortfall returns NEGATIVE margin_free), it cross-checks LIVE `account_info().margin_free` against an independent `mt5.order_calc_margin()`; if affordable, downgrades reject→transient so the worker proceeds/retries instead of killing the trade. Fail-safe keeps the reject on any error or genuinely-broke account. **+5 tests, 112 pass.** Committed + pushed.
- **Account change recorded:** personal account is now **448196 / FusionMarkets-Live / 6,500 SGD** (was 459166); prop unchanged (**20047930 / FundingPips-SIM1 / $50k**). Source of truth = MT5 terminal saved-default login + matching `.env MT5_LOGIN`, not docs.

## Next actions
1. **Deploy: `/update layer3` (2 = Prop)** then Ctrl+C + re-run `worker_prop.py` on VPS #3. This ships BOTH the session-19 diagnostic log AND the session-20 self-healing guard. (`git pull` alone does NOT reload the worker.)
2. **Verify VPS #2 `.env` `MT5_LOGIN=448196`** (matches the new personal account's terminal saved-default). If it still says 459166 the personal worker fatal-exits on the hard account guard → no trades. Restart `worker_personal.py` after fixing.
3. After both workers restart: `/resume` + `/rearm`, then watch the next signal **via Telegram** (`/positions`, `/equity`), not the desktop GUI.
4. **Still-carried-over from sessions 15–18 (not done):** `/update layer2` (Telegram changes incl. `/phase1` fee-anchor reset). Personal worker may still be on pre-session-17 code — confirm `/equity` Trading Fee behaves per-cycle after restart.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (work on `main`)

## Open items
- **Root cause of the $0 NO_MONEY still unconfirmed.** The guard makes it non-fatal, but if you want certainty, the next $0 reject will now log `account_info free=50000` vs `check free=0` (diagnostic) and a `NO_MONEY OVERRIDE` warning line when the guard fires. Watch the prop worker log after deploy.
- Pre-existing uncommitted working-tree edits (`docs/Project_Overview.md`, `docs/System_Architecture.md`) were left untouched — not mine this session.

## Pick up here
Deploy `/update layer3` (Prop) + restart `worker_prop.py`, confirm VPS #2 `.env` = 448196, then `/resume`+`/rearm` and watch the next signal land via Telegram.
