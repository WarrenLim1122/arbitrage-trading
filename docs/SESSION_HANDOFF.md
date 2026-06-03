# Session handoff — deal-history timezone fix (journal lag / `(est.)` close alerts)

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single Claude agent + Warren (operator). Warren deploys via Telegram `/update`; Claude edits + pushes to `main`.

## Status — updated 2026-06-03
- **This session (16) — SHIPPED + pushed to `main`, NOT yet deployed.** Commits `884eb02` `4a2222a` `855a421`. Full detail in CLAUDE.md §Current State → Session 16.
- Root cause of the journal-queue / `(est.)` problem was found: `mt5.history_deals_get` filters on `deal.time` which is in **server tz (UTC+2/+3), not UTC**; the query `to_dt` was `UTC-now + secs`, excluding fresh deals for hours. Fixed `to_dt = UTC-now + 1 day` in both `journaling_worker._get_deals` and `_worker_core._build_deal_pnl_reply`. Read-window only — no execution/sizing/kill/connection code touched. Memory written: `mt5-deal-history-server-timezone`.
- `msg_position_closed` (Telegram text) was traced and left **unchanged** — it already renders real P&L/exit/fee whenever `deal['found']`; `(est.)` is only the missing-deal fallback. 3 new tests pin this. 107 tests pass total.
- **Still NOT deployed from session 15:** symbol mapper (`575af7d`) + webhook pine (`8c77009`). Those Layer 1/2 + `_worker_core.py` changes are on `main` but never ran on the VPSes.

## Next actions
1. **Deploy Layer 3** (covers both sessions 15 + 16): `/update layer3` ×2 — `_worker_core.py` AND `journaling_worker.py` changed. Workers need a true **Ctrl+C restart**, not just `git pull`. No `pyproject.toml` change → no `uv sync`.
2. **Deploy Layer 2** (session 15 L1/2 symbol-mapper derivations): `/update layer2`.
3. **Verify the fix live:** close one trade → confirm the Position Closed alert arrives within ~30s with real P&L, real exit, Trading Fee, no `(est.)`, and NO "📋 Journal Queued" message.
4. **After workers restart, run `/checksymbols`** — capture which of the 33 canonicals resolve on Fusion (VPS #2) vs FundingPips (VPS #3). Most exotic/NDF/pegged expected MISSING.
5. **Warren, on TradingView:** paste `layer0/Nadaraya-Watson Webhook INDICATOR.pine` into the NW indicator, save, recreate the alert (Any alert() function call → webhook URL) so it sends the 14-field payload.
6. **Warren, manual:** `rmdir "Suggest To Delete"` — files deleted/committed but the empty dir shell remains (env EPERM on root dir deletion).

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`, HEAD `855a421`, pushed)

## Open items
- Lower-priority queued (not started): (A) retrofit msgs 1-19 with the three 20-37 layout fixes; (B) discuss overall message-structure spec with Warren + write one paragraph; (C) fold into CLAUDE.md. Folder reorg (prior handoff `accd561`) lower still.
- Only arm a TradingView alert for a pair `/checksymbols` shows FOUND on the trading broker — else the signal dies at Layer 3 (two-gate model).
- Pre-existing unstaged edits remain in `docs/Project_Overview.md` + `docs/System_Architecture.md` (not from this session — left untouched).
- Optional hardening: set `MT5_SERVER_UTC_OFFSET_HOURS` in each worker `.env` as belt-and-suspenders for the deal.time tz (not needed now that `to_dt` is wide).

## Pick up here
Deploy Layer 3 (`/update layer3` ×2, true worker restart), then close one trade to confirm the close alert lands ≤30s with real P&L and no `(est.)` — that live close is the one thing that couldn't be validated locally. Then `/checksymbols` for the session-15 symbol tables.
