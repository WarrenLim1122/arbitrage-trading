# 07 — Telegram Command & Message Spec

Mirror the reference look exactly (Warren's bar: [[telegram-reporting-standards]]). All message text
lives in `receiver/messages.py` as pure builders; `telegram_bot.py` only decides when to call them.

## Formatting standard (every alert + reply)
- A `━`×12 rule, a **bold title**, another rule, then `Label: value` rows.
- Telegram uses a proportional font → **space-padding never aligns; the colon is the anchor.** Render
  rows as `Label: value`; drop rows with empty values.
- Helpers to port from `layer2/telegram_handlers.py`: `_cmd_header`, `_msg_aligned_rows`,
  `_msg_signed_money`, `_msg_positions_lines`.

## Currency rules (do NOT break — there's a test for this)
- **Personal account renders in the MT5-reported currency** (currently SGD). Pass `account_currency`
  from the `equity` reply into every money render. Switching to GBP/EUR needs no code change.
- **Money sign goes BEFORE the symbol** in alerts: `-SGD 12.50`, `+SGD 30.00` (use `_msg_signed_money`).
- **Forex prices (Entry/SL/TP) carry NO currency symbol** — they're quotes. Use `fmt_price`.
- There is no prop side here, so **no hardcoded `$`** anywhere on the money path. (`$` only ever appears
  if the account currency itself is USD.)

## Command registry (the standalone set)

**Keep & adapt from reference:**
| Command | Args | Behavior |
|---|---|---|
| `/start` | — | set `active=true`; resume taking signals. |
| `/stop` | — | set `active=false`; stop taking signals (positions untouched). |
| `/status` | — | mode, baseline, risk %, active/halt state, open positions, day-start equity. |
| `/equity` | — | balance/equity/P&L + the **all-in trading fee** (runs the full-history fee scan; `want_fee=true`). |
| `/positions` | — | open personal positions with live P&L. |
| `/setbaseline` | `<amount>` | set the immutable `personal_baseline` (SGD). The risk anchor. |
| `/setdeposit` | `<amount>` | actual capital — **reporting/% only**, zero effect on sizing. |
| `/setwindow` | wizard | set SGT trading window (`current`/`next`). |
| `/setdayroll` | `<HH:MM>` | set the SGT session reset time. |
| `/closepair` | wizard | manually close + suppress a pair. |
| `/resumepair` | — | clear a manual pair suppression. |
| `/checksymbols` | — | per-broker SUPPORTED/FOUND/MISSING (not advertised in `/help`). |
| `/news` | — | upcoming high-impact events in the filter window. |
| `/health` | — | worker reachable? MT5 connected? account match? |
| `/emergency` | wizard | force-close everything now. |
| `/update` | wizard | deploy helper (prints the redeploy steps — see `09`). |
| `/help`, `/cancel` | — | help text; abort a wizard. |

**NEW (standalone-specific):**
| Command | Args | Behavior |
|---|---|---|
| `/mode` | `[conservative\|aggressive]` | no arg → show active mode; arg → switch (takes effect next signal). |
| `/setrisk` | `<mode> <pct>` | set a mode's `risk_pct` (e.g. `/setrisk aggressive 2.0`). |
| `/halts` | — | show daily/overall DD %s, current halt state, day-start equity. |
| `/setdailydd` | `<pct>` | set the daily DD halt %. |
| `/setoveralldd` | `<pct>` | set the permanent overall DD halt %. |
| `/resume` | — | clear today's daily halt (sets `soft_kill_override_day`); does NOT clear a permanent halt. |
| `/rearm` | — | clear the override so the daily halt can fire again today. |
| `/clearhalt` | — | clear a **permanent** halt (explicit, deliberate). |

**DROP entirely (prop/phase/hedge — no analog):** `/phase1`, `/phase2`, `/changepropfirm`,
`/consistency`, `/propfirm`, `/setmaxpos` keep (rename to personal positions), `/maxpos` keep.

## Required alert messages (`msg_*`)
Port these, single-leg + SGD-aware: `msg_trade_opened` (direction, lots, entry/sl/tp, risk in SGD,
realized RR, mode), `msg_position_closed` (real net P&L when `deal_pnl.found`, else `(est.)`),
`msg_signal_blocked_p_halt`, `msg_signal_skipped_halted`, `msg_signal_suppressed`,
`msg_signal_skipped_max_pos`, `msg_signal_skipped_dedup`, `msg_signal_blocked_algo_disabled`,
`msg_baseline_missing`, `msg_geometry_reject`, `msg_signal_not_placed_preflight`,
`msg_order_not_filled`, `msg_halt_daily`, `msg_halt_overall`, `msg_new_session_auto_resumed`,
`msg_worker_offline`, `msg_worker_back_online`, `msg_curfew_close`, `msg_news_pre_close`,
`msg_internal_error`, `msg_contract_query_failed`.

**Drop:** all `msg_kill3/4/5*`, all `msg_*phase1*`/`*phase2*` stage/consistency variants, the dual-leg
pre-flight two-leg block (becomes single-leg).
