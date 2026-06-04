# Session handoff — prop XAUUSD "Signal Not Placed" diagnosed + order_check diagnostic log

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single agent (Claude) on the arbitrage-trading repo; Warren operates the VPSes/Telegram.

## Status — updated 2026-06-04
- Diagnosed why a prop **XAUUSD LONG-hedge** signal showed **"Signal Not Placed"**: prop `order_check` returned **NO_MONEY (10019)** with `Needs $0.00 / Free $0.00` on the $50k account. Concluded **bogus/degenerate read, not a real margin shortfall** — Phase-1 gold lot ≈ 0.27 lots needs ~$4-7k vs $50k free. Ruled out: lot-too-big, narrow-SL (would be 10016 "Invalid stops"), wrong account. Both legs gate together by design, so the bad prop reject also blocked the (✅ can-fill) personal leg.
- Leading cause: **interactive MT5 GUI left logged into the prop account while the worker runs** → coexisting sessions make the worker read `account_info().margin_free = 0`. Unconfirmed; the new log will prove/disprove.
- **New prop account confirmed via .env on VPS #3:** `MT5_LOGIN=20047930`, `MT5_SERVER=FundingPips-SIM1`, $50k demo (replaces old `12250900`/`FundingPips2-SIM`). CLAUDE.md updated.
- **Shipped:** diagnostic `logger.info` in `_build_order_check_reply` (`layer3/_worker_core.py`) — dumps `margin_req/free/bal/eq` + live `account_info` `login/free` cross-check; wrapped in try/except (non-fatal). Committed + pushed to `main`. Tests **107 pass**.

## Next actions
1. **Warren deploys + retests** (this is the live verification we paused for): close desktop MT5 on VPS #3 → `/update layer3` (choose **2 = Prop**) → Ctrl+C and re-run `worker_prop.py` (git pull alone won't reload) → `/resume` + `/rearm` → watch the next signal.
2. **On the next signal, read the prop worker PowerShell log** for the new `order_check … [account_info: login=… free=…]` line. If `account_info free≈50000` while `check free=0` → dual-session theory confirmed (keep desktop MT5 closed). If both read 0 → different root cause, investigate further.
3. Carry-over deploys still pending from sessions 15–18 (see CLAUDE.md §Next Session): `/update layer2` + `/update layer3` ×2; personal worker (VPS #2) still on pre-session-17 code.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`)

## Open items
- Whether the dual-session hypothesis is correct — waiting on the next signal + the new log line to confirm.
- No AGENTS.md adapter exists; if Warren wants Codex parity, invoking `claude-codex-setup` would create one.

## Pick up here
Wait for Warren's report on the next signal after he restarts the prop worker (desktop MT5 closed). If it enters cleanly → done. If "Signal Not Placed" recurs, pull the new `order_check` log line from VPS #3 and compare `account_info free` vs `check free` to confirm/refute the dual-session cause.
