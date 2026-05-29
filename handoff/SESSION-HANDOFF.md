# Session handoff — Apply msgs 20-37 layout fixes to msgs 1-19 + structure discussion

> Persistent resume file. CLAUDE.md (auto-loaded) has the live system state.
> Delta only — read this for what's queued; read CLAUDE.md for everything else.

**Role:** Single Claude Code agent helping Warren retrofit the Telegram message
layout fixes (already applied to templates 20-37) onto templates 1-19, then
have a deep discussion with him on overall message structure, then update
CLAUDE.md. Live trading state has not changed.

---

## What shipped this session (do NOT re-do)

Three commits land in `main` and need `/update layer2` on Telegram to take effect:

- **b7da59a** (2026-05-29) — three layout fixes wired into templates 20-37:
  1. `_msg_aligned_rows` now renders `Label: value` instead of space-padded
     alignment (Telegram's proportional font never aligned the spaces).
  2. Every ticket renders as `#<id>` (e.g. `Ticket: #987654321`).
  3. Personal Risk/Reward/P&L/Commission/Margin/Equity render in the
     MT5-reported account currency only — dropped the dual `$X (≈ SGD X)`
     form. Prop stays `$`. Forex prices (Entry/SL/TP) stay as raw quotes
     with no currency symbol.
  Also:
  - `_msg_pers_money_dual` renamed to `_msg_pers_money_acct` and rewritten to
    return only the account-currency value.
  - `_msg_order_check_leg_line` now takes `currency` so pre-flight margin/free
    use SGD for personal, USD for prop.
  - `msg_signal_not_placed_preflight` gained a `pers_currency` parameter;
    `logic_core.py` line 1512 passes `pers_info.get("account_currency", "USD")`.
  - Catalog demos for msgs 20, 21, 23 hardcoded `pers_currency="SGD"` so
    `/messages2` previews show the live layout.
  - `tests/layer2/test_currency_display.py` renamed assertions to match the
    new helper. 90/90 pass.
- **c212ce5** (2026-05-29) — `msg_trade_opened` and `msg_position_closed` now
  render `Ticket: #<id>` directly beneath the side header
  (`Personal Signal ↑ LONG` / `Prop Hedge ↓ SHORT`), instead of after
  Risk/Reward/RR. Warren's mental model: ticket IS the trade's name.

**Verified rendering** via local `~/.local/bin/uv run --extra dev` driving
the catalog lambdas; pasted into Warren's Telegram to confirm visually.

---

## TASK A — Retrofit templates 1-19 with the same three fixes

The 18 templates in `msg_*` lines ~2362-2796 of `layer2/telegram_handlers.py`
were redesigned earlier (`8940848`) but PRE-DATE this session's three layout
decisions. They likely still have:

- Hand-built spacing/padding inside f-strings (no `_msg_aligned_rows` call).
- Plain ticket renderings (no `#` prefix where tickets appear).
- Mixed `$` rendering that doesn't respect MT5 account currency (e.g.
  `_snapshot_positions_str` hardcodes USD inside `zmq_helpers.py`).

### Concrete starting points

1. Open `layer2/telegram_handlers.py` and read every `msg_*` function from
   line ~2362 to ~2796. The 19 are:
   `msg_worker_offline`, `msg_worker_back_online`, `msg_algo_trading_disabled`,
   `msg_algo_trading_restored`, `msg_new_session_auto_resumed`,
   `msg_curfew_close`, `msg_mismatch_resolved`, `msg_news_window_cleared`,
   `msg_news_pre_close`, `msg_phase1_stage_reached`, `msg_kill1_phase1`,
   `msg_kill2_phase1`, `msg_kill4_phase1_passed`, `msg_kill2_phase2plus`,
   `msg_kill1_phase2plus`, `msg_kill3_daily_profit_cap`,
   `msg_kill4_phase1_via_target`, `msg_kill4_phase2plus`, `msg_kill5_consistency`.
2. For each, identify hand-formatted label rows that should switch to
   `_msg_aligned_rows([...])`. Don't force the helper everywhere — some
   templates have prose-style bodies that read better unchanged.
3. The Kill-2+ templates take a `pos_str` pre-built by
   `_snapshot_positions_str` in `layer2/zmq_helpers.py`. That helper hardcodes
   USD. If we want personal positions in SGD inside kill alerts, that helper
   needs the personal account currency threaded in. Decide WITH Warren first
   whether kill-alert position rows should be per-account-currency or stay
   USD-only — kills are prop-driven so USD might be intentional.
4. Render the full `/messages` (page 1) preview locally before pushing:
   ```bash
   cd "<repo>"
   TELEGRAM_BOT_TOKEN=stub TELEGRAM_CHAT_ID=0 \
     ~/.local/bin/uv run --extra dev python -c "
   from layer2 import telegram_handlers as m
   for name, _, render in m.MESSAGE_CATALOG[:19]:
       print(f'==== {name} ====')
       print(render())
       print()
   "
   ```
5. Commit + push, then tell Warren to run `/update layer2` + `/messages`.

---

## TASK B — Deep discussion on overall message structure

Warren explicitly asked for this. Before writing any code for TASK A, open
the conversation with concrete proposals on:

1. **Information hierarchy per message.** What goes at the top
   (identity/ticket), the middle (numbers), the bottom (context/recovery)?
   The 20-37 retrofit picked one answer: header → ticket → levels → risk
   → context. Validate this with Warren and decide whether to apply it
   uniformly across 1-19 (e.g. should kill alerts also have a "ticket" or
   identifier line at the top?).
2. **When to use `_msg_aligned_rows` vs prose.** The helper is great for
   key/value blocks (Size, Entry, SL, TP). It's awkward for sentence-style
   blocks (e.g. "Take Profit reached at 12:34 SGT — auto-resume next
   session"). Surface a rule: aligned rows for >=3 paired metrics, prose
   otherwise.
3. **Currency labeling rules.** Spell out the rule so the next change
   doesn't have to be re-derived: Account-balance/P&L/risk numbers → MT5
   account currency. Prices (entry/SL/TP) → no currency symbol. Prop side
   → always `$` (CLAUDE.md hard constraint). Personal side → whatever MT5
   reports.
4. **Ticket placement convention.** Warren confirmed ticket-under-header
   for msgs 20-21. Does that also apply to msgs 5, 6, 7 (curfew close,
   mismatch resolved, news pre-close — which mention positions)? Or only
   to "trade-event" messages where there's exactly one ticket-per-side?

Goal of the discussion: a one-paragraph **message structure spec** that
goes into CLAUDE.md or TECHNICAL.md so future redesigns don't have to
rederive these decisions.

---

## TASK C — Update CLAUDE.md before Warren closes session

CLAUDE.md was refreshed last session (commit `accd561`) but is now stale:

- The `🔔 Next Session` block still points to the OLD queued tasks
  (folder reorg + `$`/currency audit). The currency audit has now been
  done as part of this session's work — the dual `$ (≈ SGD)` form is gone
  for personal, replaced with single-currency rendering. The folder reorg
  is still pending but is a lower priority than TASK A/B above.
  → Rewrite the `🔔 Next Session` pointer to point to this handoff and
    the THREE tasks (A retrofit, B structure discussion, C CLAUDE.md
    refresh — i.e. this very task).
- The `Current State` section lists commits `5d1f58a` through `4f7a69b`
  under "Layer 2 telegram-message consolidation". Add `b7da59a` and
  `c212ce5` to that list.
- The **Hard Constraints** section says *"Both live MT5 accounts MUST be
  USD-denominated."* This is now factually wrong: per
  `~/.claude/projects/.../memory/sgd-usd-account-currency.md`, Warren
  reversed the decision 2026-05-23 (personal=SGD, prop=USD). The Layer 2
  retrofit shipped this session makes the personal SGD display work
  end-to-end. The CLAUDE.md text should be updated to:
  *"Personal account currency is whatever MT5 reports (auto-detected;
  currently SGD on the live Fusion Markets account). Prop account MUST
  stay USD-denominated (hard constraint — prop-firm rule). All Telegram
  alerts auto-format personal-side money in the MT5-reported currency."*
  Also revise the surrounding paragraph that said `$` was hardcoded
  everywhere — that's no longer true for templates 20-37.
- The "partially addressed" close-alert P&L pending note in the prior
  handoff is now fully addressed for msg 21 (msg_position_closed shows
  Reason / Trade P&L / Commission as separate aligned rows in the right
  currency). Mark it done.

---

## Open items NOT for this session

- **Folder reorganization** (deletion table from the previous handoff,
  commit `accd561`) — still queued. The deletion list and reasoning are
  in the git history of that prior handoff; do NOT redo the survey work
  if Warren returns to this later. The deletions are still safe:
  `.DS_Store`, broken `.claude/skills/skill-creator` symlink,
  `skills-lock.json`, the four 0-byte log files, `logs/layer2_2026-05-16.log`,
  and (with confirmation) `docs/AI_Workflow.md`,
  `docs/superpowers/*.md`, `layer0/TEST-ONLY 15m Loop INDICATOR.pine`,
  `scripts/backfill_journal.py`.
- **Live trading state** (Layer 3 cutover deploy on both VPSes) — still
  awaiting `git pull` + worker start on VPS #2/#3. Unchanged.

---

## Pick up here

Open `layer2/telegram_handlers.py` line ~2362, scan msgs 1-19, then start
TASK B's discussion with Warren before writing any code. The structure spec
that comes out of TASK B should land in CLAUDE.md as part of TASK C.

---

## Running state

- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: main only

---

*Last updated: 2026-05-29 after commit c212ce5.*
