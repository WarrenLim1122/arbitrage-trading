# Telegram message system & commands

**All** Telegram text — pushed alerts AND on-demand command output — lives in
`layer2/telegram_handlers.py` (~4200 lines). `logic_core.py` never builds message strings; it
calls `telegram_handlers.msg_*()` and `_cmd_*` handlers. To change any wording, edit only this file.

`/messages` and `/messages2` print the live catalog on the phone (paginated to survive Telegram's
~20-message burst flood cap). The catalog is `MESSAGE_CATALOG` (`telegram_handlers.py:3826`).

## Formatting standard (the de-facto spec)

Every alert and command reply uses the same shape: a `━`×12 rule, a bold title, another rule, then
`Label: value` rows. Helpers:

| Helper | Line | Purpose |
|---|---|---|
| `_MSG_SEP = "━" × 12` | 2568 | the separator rule used by all `msg_*` alerts |
| `_cmd_header(title)` | 81 | header for on-demand command replies (same look as alerts) |
| `_cmd_pos_block(label, positions, err, currency, detail, show_pnl)` | 94 | render one account's open positions (shared by `/positions`, `/emergency`, `/closepair`, `/stop`, `/resume`) |
| `_msg_aligned_rows(rows)` | 2601 | render `[(label, value)]` as `Label: value`; drops rows with falsy values. **Telegram uses a proportional font — space-padding never aligns; the colon is the anchor.** |
| `_msg_positions_lines(positions, currency)` | 2583 | compact one-line-per-position (used in news pre-close) |
| `_msg_side_label(side)` | 2618 | `"prop"→"Prop Hedge"`, else `"Personal Signal"` |
| `_msg_order_check_leg_line(label, chk, currency)` | 2623 | per-leg pre-flight reject block |

## Currency rules (do not break these)

- **Prop side is always USD** — hardcoded `$`. Prop is a hard USD constraint.
- **Personal side renders in the MT5-reported account currency** (currently SGD), passed in as
  `pers_currency` from the personal worker's `account_currency`. Switching to GBP/EUR needs no code.
- **Forex prices (Entry/SL/TP) carry no currency symbol** — they're quotes, not money. Use
  `_fmt_price(symbol, price)` (`state.py:422`) for price formatting (JPY=3dp, XAU=2, XAG=4, else 5).

Money helpers:

| Helper | Output | Use |
|---|---|---|
| `_msg_signed_money(value, currency)` (2571) | `+$12.50` / `-SGD 12.50` — **sign BEFORE symbol** | all P&L / commission / signed amounts in `msg_*` |
| `state._money(amount, currency, signed)` (`state.py:441`) | `$12.50` / `SGD 12.50`; signed → `$+12.50` (sign AFTER) | command-side plain amounts |
| `_msg_pers_money_acct(ticker, value, currency, rate, signed)` (2672) | personal money in account currency | personal-side rows |
| `_msg_split_pers_amount(ticker, value, rate)` (2664) | recover `(usd, acct_ccy)` from a geometry figure | dual-currency |

`state._ccy_prefix(currency)` → `"$"` for USD else `"SGD "` (ISO code + space). The audit invariant:
**no personal-context `$` and no sign-after-`$` (`$+`/`$-`) should appear** — `$+`/`$-` would mean a
`_money(..., signed=True)` leaked into an alert. Standards memory: [[telegram-reporting-standards]].

## Message catalog (`msg_*` functions)

Grouped by trigger. Each is a pure string-builder; `logic_core` decides when to call it.

**Worker / system state** (`telegram_handlers.py` ~2689–2778)
`msg_worker_offline`, `msg_worker_back_online`, `msg_algo_trading_disabled`,
`msg_algo_trading_restored`, `msg_new_session_auto_resumed`, `msg_curfew_close`.

**Mismatch / news** (~2797–2916)
`msg_mismatch_resolved`, `msg_news_window_cleared`, `msg_news_pre_close`.

**Kill alerts** (~2918–3178)
Phase 1: `msg_phase1_stage_reached`, `msg_kill1_phase1` (daily loss), `msg_kill2_phase1` (overall),
`msg_kill4_phase1_passed`, `msg_kill4_phase1_via_target`.
Phase 2+: `msg_kill1_phase2plus`, `msg_kill2_phase2plus`, `msg_kill3_daily_profit_cap`,
`msg_kill4_phase2plus`, `msg_kill5_consistency`.

**Trade lifecycle** (~3179–3530)
`msg_trade_opened` (phase-aware context; dual-currency personal Risk/Reward),
`msg_position_closed` (real net P&L when the deal is found, else `(est.)`),
`msg_signal_not_placed_terminal`, `msg_signal_not_placed_preflight`, `msg_order_not_filled`.

**Signal blocked / skipped** (~3532–3670)
`msg_signal_blocked_p_halt`, `msg_signal_skipped_halted`, `msg_signal_suppressed`,
`msg_signal_skipped_max_pos`, `msg_signal_blocked_algo_disabled`, `msg_signal_blocked_generic`,
`msg_geometry_reject`.

**Errors** (~3671–3795)
`msg_internal_error`, `msg_contract_query_failed`, `msg_baseline_missing`,
`msg_invalid_contract_data`, `msg_tp_distance_zero`, `msg_dispatch_failed`.

> The Trade Opened / Trade Closed layouts are also described in `TECHNICAL.md §Telegram Alert Formats`.
> When you change a `msg_*`, check whether it's pinned by a test in `tests/layer2/` (e.g.
> `test_position_closed_alert.py`, `test_currency_display.py`).

## Command registry (`telegram_handlers._run_bot`, ~4100–4216)

**Trading control:** `/phase1` (wizard), `/phase2` (wizard), `/stop`, `/resume`, `/rearm`,
`/setmaxpos`, `/maxpos`.
**Prop-firm config:** `/changepropfirm` (wizard), `/setbaseline <amount>` (risk anchor),
`/setdeposit <prop|personal> <amount>`, `/propfirm`, `/consistency`.
**Reporting:** `/status`, `/equity` (runs the full-history fee scan), `/positions`, `/pnl`,
`/health`, `/checksymbols`, `/news`, `/blackboard`, `/checkaccount`.
**Pair control:** `/closepair` (wizard), `/resumepair`.
**Window:** `/setwindow` (wizard).
**Ops:** `/update` (wizard — deploy helper, see [deployment.md](deployment.md)), `/emergency`
(wizard — force close), `/help`, `/messages`, `/messages2`, `/cancel`.

Wizards are `ConversationHandler`s and all set `allow_reentry=True` (session 17) so re-sending the
command mid-conversation re-prompts instead of being silently ignored. `/cancel` aborts any wizard.

> `/checksymbols` exists and works but is intentionally **not** advertised in `/help` (see memory
> [[checksymbols-and-pair-registry]]).
