# Session handoff — Layer 2 message retrofit + folder reorg + deal-wait fix

> Persistent resume file. CLAUDE.md (auto-loaded) has the live system state.
> Delta only — read this for what changed this session and what's queued next.

**Role:** Single Claude Code agent helping Warren with Layer 2 Telegram message
fixes and a folder reorganization. Live trading state has not changed.

## Status — updated 2026-05-29

Four commits shipped, all on `main`, all pushed. **Warren has NOT run `/update layer2`
yet** — the three Layer 2 commits below all need that deploy to land on the live bot.

- **`7fbb14a`** — Layer 2: apply msgs 20-37 layout fixes to msgs 1-19.
  - Hand-padded `<b>Label</b>\n${value}` blocks across kill / phase-1 / mismatch
    templates now render through `_msg_aligned_rows([...])` as `Label: value`.
  - `_snapshot_positions_str` (zmq_helpers.py) and `_msg_positions_lines`
    (telegram_handlers.py) now take `pers_currency`; personal P&L renders in
    the MT5 account currency (SGD on Fusion), prop stays USD.
  - `logic_core.py` threads `pers_currency = _pers_result.get("account_currency", "USD")`
    into curfew_close, news_pre_close, and all five Phase-2+ kill paths.
  - `_demo_pos_str` updated so `/messages` previews match live SGD/$ rendering.
  - No `#<id>` ticket changes — none of msgs 1-19 carry a ticket.
- **`7f4ade0`** — Layer 2: bracket every alert title with top + bottom ━ rule.
  - `_MSG_SEP` shortened `"━" * 18` → `"━" * 12` (Warren's iPhone screenshot showed
    18 wrapping in portrait).
  - All 37 templates now render `_MSG_SEP\ntitle\n_MSG_SEP\n\nbody`. Bulk applied
    via regex prepend in 36 standard sites + manual edit at the lone
    `msg_position_closed` site (line ~3053).
- **`5229fd8`** — chore: reorganize repo + collect deletion candidates.
  - `scripts/` now has `vps-setup/` (setup_worker_*.ps1) and `dev-tests/`
    (test_firebase_write.py, test_journal_dryrun.py) subfolders.
  - New top-level `Suggest To Delete/` folder collects 13 deletion candidates
    + a README.md explaining each item. 5 tracked items moved via `git mv`
    (so history is preserved); 8 untracked items just `mv`d (gitignored).
  - README.md scripts/ row updated.
  - All paths in `Suggest To Delete/` are documented in
    `Suggest To Delete/README.md` with rationale. Warren reviews; deletes
    later with `git rm -rf "Suggest To Delete/"` in one commit.
- **`7a78cc7`** — Layer 2: close alert waits for MT5 deal so Telegram + journal P&L match.
  - Root cause: 30 s equity monitor captured stale `pos_data["profit"]` from
    the previous tick (missing commission/swap, stale by tens of seconds);
    `_query_deal_pnl` was called inline and often returned `found: False`
    (MetaQuotes Demo lags 2-3 h; Fusion occasionally races MT5 indexing);
    fallback path slapped `(est.)`. Journal pipeline eventually wrote the
    real number — Telegram and journal diverged.
  - Fix: `_detect_closes()` now polls `_query_deal_pnl()` every 30 s tick
    for any pending close whose deal hasn't surfaced; flushes the alert
    AS SOON AS both deals land.
  - New constant `_CLOSE_DEAL_TIMEOUT = 600` (10 min) hard cap for the
    deal-wait branch; existing `_CLOSE_WAIT_SECONDS = 120` retained for
    orphan grace.
  - `_send_close_alert()` refactored to receive pre-fetched deals from the
    flush loop instead of querying inline.
  - `msg_position_closed` docstring updated; the `(est.)` fallback path
    only fires now when MT5 history still hasn't surfaced the deal after
    10 min (typically MetaQuotes Demo).

90/90 tests pass after every commit. Folder reorg verified — no production
paths affected.

## Next actions

1. **Warren runs `/update layer2`** on Telegram to deploy `7fbb14a`, `7f4ade0`,
   `7a78cc7`. Then `/messages` + `/messages2` to visually verify the bracketed
   header + colon-row format + correct currency on all 37 templates.
2. **Warren reviews `Suggest To Delete/`** (open `Suggest To Delete/README.md`
   first — per-item rationale). When done, `git rm -rf "Suggest To Delete/"`
   removes everything in one commit.
3. **Verify the deal-wait fix on a real close** — when the next trade closes,
   check that the Telegram Trade P&L value equals the journal dashboard
   number byte-for-byte (instead of the prior `(est.)` divergence).
4. **TASK B from prior handoff still pending** — deep discussion with Warren on
   overall message structure spec (information hierarchy, when to use aligned
   rows vs prose, currency labeling rules, ticket placement convention). Lands
   as a one-paragraph spec in CLAUDE.md or TECHNICAL.md.
5. **TASK C from prior handoff still pending** — refresh CLAUDE.md once the
   structure spec from TASK B is settled. CLAUDE.md "Current State" section
   needs `7fbb14a`, `7f4ade0`, `5229fd8`, `7a78cc7` added to the commit list.
   Hard Constraints section still has the stale "Both live MT5 accounts MUST
   be USD-denominated" wording — replace with the SGD-personal / USD-prop
   reality (per memory/sgd-usd-account-currency.md, 2026-05-23 reversal).

## Running state

- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: main only (no worktrees)

## Open items

- **Layer 3 cutover deploy** still pending on VPS #2/#3 (`git pull` + worker
  start with `MT5_TERMINAL_PATH` env on VPS #2). Unchanged from prior session;
  see CLAUDE.md §Current State.
- **`logs/layer2_2026-05-29.log` and `logs/layer3_worker_2026-05-29.log`** were
  intentionally NOT moved into `Suggest To Delete/logs/` (created today; might
  be touched by a local process). Re-evaluate on a later session.
- **Possible follow-up Warren mentioned**: if MetaQuotes Demo's 2-3 h lag
  causes too many `(est.)` prop-side alerts after the deal-wait fix, wire
  Telegram `editMessageText` so the journal pipeline can EDIT the original
  Telegram message with the corrected P&L when the deal eventually lands.
  Not started — Warren said he'll decide next session if needed.

## Pick up here

Verify Warren has run `/update layer2` and seen the new bracketed headers /
correct currency / no-`(est.)`-on-deal-found rendering on his phone. If
he confirms it works as expected, move to TASK B (message structure spec
discussion) from the prior handoff. If he reports a problem with the
deal-wait fix (e.g. alerts arriving too late, or some path missing data),
debug at `layer2/logic_core.py` `_detect_closes()` / `_send_close_alert()`
and `_query_deal_pnl` in `zmq_helpers.py`.

---

*Last updated: 2026-05-29 after commit `7a78cc7`.*
