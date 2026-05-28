# Session handoff — Folder reorganization + Telegram message dollar-sign audit

> Persistent resume file. CLAUDE.md (auto-loaded) has the live system state.
> This file contains ONLY the two things queued for the next session.

**Role:** Single Claude Code agent helping Warren tidy the project repo + audit
the Telegram message-builder text for consistency. Live trading state has not
changed — Layer 2 message-builder consolidation completed and shipped over
sessions ending 2026-05-28 and 2026-05-29.

---

## What just shipped (do NOT re-do)

- **5d1f58a** (2026-05-28) — All Telegram message text consolidated into
  `layer2/telegram_handlers.py` as `msg_*()` functions. Logic_core stays pure
  orchestration. New `/messages` and `/messages2` Telegram commands print the
  catalog with trigger conditions for review on phone.
- **1b03ddf** (2026-05-28) — Paginated `/messages` so all 37 templates survive
  Telegram's ~20-message burst flood cap.
- **8940848** (2026-05-28) — Applied Warren's redesigned templates 1-19
  (header + ━ separator + labeled blocks). New shared helpers: `_MSG_SEP`,
  `_msg_signed_money`, `_snapshot_positions_str` reformatted.
- **4f7a69b** (2026-05-29) — Applied redesigned templates 20-37. New helper
  `_msg_aligned_rows`. Signature simplifications for `msg_trade_opened`,
  `msg_signal_blocked_generic`, `msg_order_not_filled`. Audit confirmed
  **zero orphans** across all 37 messages.

**Deploy: `/update layer2` in Telegram, then `/messages` and `/messages2` to
review the redesigned templates on the live bot.**

---

## TASK A — Folder reorganization (deferred from 2026-05-28 session)

Earlier in the prior session I surveyed the repo and presented Warren a
deletion + reorganization plan. He paused that work to redesign the Telegram
messages first. Resume from this exact recommendation set.

### Files to delete (high confidence — safe, no decisions needed)

```
.DS_Store                          # macOS Finder metadata, gitignored
.claude/skills/skill-creator       # BROKEN symlink (target doesn't exist locally);
                                   # real skill is global at ~/.agents/skills/skill-creator
skills-lock.json                   # lockfile for the broken skill-creator above
logs/layer2_2026-05-23.log         # 0 bytes
logs/layer2_2026-05-24.log         # 0 bytes
logs/layer2_2026-05-28.log         # 0 bytes
logs/layer3_worker_2026-05-23.log  # 0 bytes
logs/layer2_2026-05-16.log         # 366 bytes, stale dev-machine log
                                   # (real logs live on VPS #1)
```

Keep `logs/.gitkeep` so the directory ships in a clone.
Keep `handoff/SESSION-HANDOFF.md` (this file) — it IS the resume doc.

### Files Warren needs to decide on (ask once, then act)

| Path | Recommendation | Why |
|---|---|---|
| `docs/AI_Workflow.md` | **Delete** unless portfolio piece | Slightly out of date (mentions `dd_floor.json`, sonnet 4.6); not referenced by any code/docs |
| `docs/superpowers/plans/2026-05-16-phase1-strategy.md` | **Delete or archive** | Pre-implementation plan; Phase 1 now live in `layer2/phase1_strategy.py` |
| `docs/superpowers/specs/2026-05-16-phase1-strategy-design.md` | **Delete or archive** | Same — historical design doc, served its purpose |
| `docs/superpowers/` folder | Delete if both files go | |
| `layer0/TEST-ONLY 15m Loop INDICATOR.pine` | **Delete** unless still on a TV chart | Filename says TEST-ONLY; not referenced by CLAUDE.md |
| `scripts/backfill_journal.py` | **Delete** | Hardcoded for a single past XAUUSD trade (ticket `8520846485`, 2026-05-07). Single-use only. |

### Files that must STAY (load-bearing path-wise — verified)

Cannot move without breaking systemd on VPS #1 / worker invocations on VPS #2-3 / nginx:
- `layer0/`, `layer1/`, `layer2/`, `layer3/`, `tests/`, `config/`, `secrets/`,
  `scripts/setup_worker_*.ps1`, `scripts/test_journal_dryrun.py`, `scripts/test_firebase_write.py`
- `CLAUDE.md`, `TECHNICAL.md`, `README.md`, `pyproject.toml`, `.env.example`,
  `.gitignore`, `.python-version`, `uv.lock`
- `docs/Account_Currency_Decision.md` (cited by CLAUDE.md)
- `docs/MT5_VPS_Connection_Postmortem.md` (cited by CLAUDE.md)
- `docs/Project_Overview.md`, `docs/System_Architecture.md`, `docs/README.md`,
  `docs/Sample_Logs.md`
- `layer0/1D-15m Breakout INDICATOR.pine` (LIVE per CLAUDE.md)
- `layer0/1D-15m Breakout STRATEGY.pine` (backtest companion — keep for re-validation)

### Reorganization moves to propose (after deletes)

The realistic scope is small because so many paths are baked into production:

- If both `docs/superpowers/*.md` are kept: rename `docs/superpowers/` →
  `docs/archive/` so the intent (historical reference) is obvious.
- Otherwise: no other moves. The top-level is already clean once the deletes land.

### How to execute (suggested order)

1. Re-show the deletion table from this handoff to Warren — confirm any
   "ask first" decisions.
2. Delete confirmed files via `rm` / `git rm`.
3. If `docs/superpowers/` kept → `git mv docs/superpowers docs/archive`.
4. Commit + push: `chore: drop stale files + tidy docs/`.

**Do NOT** touch `docs/Project_Overview.md` or `docs/System_Architecture.md` —
they have uncommitted local edits from before this work started that aren't
Warren's intent to ship via this session.

---

## TASK B — `$` and currency-formatting consistency audit

Warren flagged this for next session. Across the 37 redesigned `msg_*`
functions there are several places where dollar/sign formatting could be
inconsistent. Specifically check:

### 1. Sign-before-currency for P&L values

The shared helper `_msg_signed_money(value, currency='USD')` produces
`+$12.50` / `-$12.50` (sign BEFORE `$`). State module's `_money(v, "USD",
signed=True)` produces `$+12.50` / `$-12.50` (sign AFTER `$`). Audit every
P&L / commission / profit display and confirm `_msg_signed_money` is used,
not `_money(..., signed=True)`.

Functions to recheck:
- `msg_position_closed._side_block` — already uses `_msg_signed_money` ✓
- `msg_kill1_phase2plus`, `msg_kill2_phase2plus`, `msg_kill3_daily_profit_cap`,
  `msg_kill4_phase1_via_target`, `msg_kill4_phase2plus` — these all take
  `pos_str` pre-built by `_snapshot_positions_str` in `zmq_helpers.py`
  (which was reformatted to sign-before-`$`). Confirm by re-rendering with
  positions that include both positive and negative P&L.
- `msg_news_pre_close` — uses `_msg_positions_lines` which uses
  `_msg_signed_money` ✓

### 2. Margin / equity values that aren't P&L

Equity / margin numbers don't have sign-before-`$` — they're always positive
and use `$X,XXX.XX`. Confirm format is plain `f"${value:,.2f}"` everywhere:
- `msg_kill1_phase1`, `msg_kill2_phase1`, `msg_kill1_phase2plus` etc:
  `Equity / $98,500.00`, `Floor / $95,000.00`, etc. — all plain `${v:,.2f}`.
- `msg_kill3_daily_profit_cap`: `Cap / +$2,000.00` — the `+` is fixed
  because cap is always positive. Verify.
- Pre-flight rejection helper `_msg_order_check_leg_line` shows
  `Needs $X margin` (plain) and `Free: -$Y` (`_msg_signed_money`). ✓

### 3. Personal account dual-currency (Issue 7)

When personal account is SGD, `_msg_pers_money_dual` shows
`$670.00 (≈ SGD 904.50)`. Only used in `msg_trade_opened`. Confirm:
- The `Risk` and `Reward` rows in `msg_trade_opened` use
  `_msg_pers_money_dual` for personal side and plain `${v:,.2f}` for prop side.
- The `Trade P&L` and `Commission` rows in `msg_position_closed` use
  `_msg_signed_money(v, pers_currency)` — for SGD this produces
  `+SGD 12.50` (sign-before-code with space). Confirm this is what Warren
  wants for SGD, or whether `+$12.50 (≈ SGD ...)` dual-currency display is
  preferred for closed trades too.

### 4. Suggested rendering check (run + visually inspect)

```bash
# In the next session, run:
~/.local/bin/uv run python3 -c "
import os, sys; sys.path.insert(0, '.')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'stub')
os.environ.setdefault('TELEGRAM_CHAT_ID', '0')
from layer2 import telegram_handlers as m

# Render every catalog entry; grep for any '\$+' or '\$-' (sign-AFTER-\$ — should be zero)
for name, _, render in m.MESSAGE_CATALOG:
    out = render()
    if '\$+' in out or '\$-' in out:
        print(f'⚠️ sign-after-\$ found in {name}')
"
```

That command should print nothing. Any hit is a place where `_money(...,
signed=True)` leaked back in.

### 5. Currency mismatches inside `msg_position_closed`

The personal side block uses `currency=pers_currency` (SGD or USD), but the
prop side block hardcodes `currency="USD"` (the hard constraint per CLAUDE.md
says prop must be USD-denominated). This is correct but easy to break — when
auditing, confirm:
- Prop block: pnl/commission rendered in `"USD"` always.
- Personal block: pnl/commission rendered in `pers_currency` (passed in by
  `_send_close_alert` from the personal worker's `account_currency` reply).
- Account Equity section: personal in `pers_currency`, prop in USD.

---

## Open items NOT for this session

- Live trading state (Layer 3 cutover deploy on both VPSes) — unchanged
  per `CLAUDE.md §Current State`. Awaiting the user to run `git pull` +
  worker start on VPS #2 and #3 with `MT5_TERMINAL_PATH` env when needed.
- The "Pending Changes — REMIND WARREN NEXT SESSION" item in CLAUDE.md
  (Telegram close-alert P&L breakdown gross+net+commission) is now
  partially addressed by the Position Closed redesign — Warren may want
  to confirm whether the redesigned format already covers his
  net+gross+commission ask, or whether further changes are needed. **Check
  CLAUDE.md before assuming this item is done.**

---

*Last updated: 2026-05-29 after commit 4f7a69b.*
