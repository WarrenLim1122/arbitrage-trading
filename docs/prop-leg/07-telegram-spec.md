# 07 — Telegram Command & Message Spec

Match the reference's visual format. All text lives in `receiver/messages.py` (pure builders); `telegram_bot.py` +
`wizards.py` decide when to call them. **Naming rule (`00`) applies — never reference another
account/system; describe everything in this system's own terms.**

## Formatting standard
`━`×12 rule, **bold title**, another rule, then `Label: value` rows (the colon is the anchor; padding
doesn't align in Telegram's proportional font). Port helpers `_cmd_header`, `_msg_aligned_rows`,
`_msg_signed_money`, `_msg_positions_lines`.

## Currency rules (there's a test)
- **Render all money in the account's reported currency** (`account_currency` from the `equity` reply) —
  auto-detected, no hardcoded symbol. `_ccy_prefix(ccy)` → `"$"` if USD else `"<ISO> "`.
- **Sign before symbol** in alerts (`+$30.00`, `-$12.50`, or `+EUR 30.00` if the account is EUR).
- **Forex prices carry NO currency symbol** — use `fmt_price`.
- Audit invariant: no `$+`/`$-` (that means a signed-after-symbol helper leaked into an alert).

## Command registry
**Challenge config (wizards):**
| Command | Behavior |
|---|---|
| `/changepropfirm` | Wizard: collect raw firm limits (target %, overall DD %, daily DD %, consistency %, min profit days, …) + `baseline_equity` + `initial_deposit`; apply buffers (`02 §4`); save; push the static-DD floor. |
| `/phase1` | Wizard: enter the `reward:risk` dollar pair → `parse_reward_risk` → `derive_stages` → save Phase 1 block; push the static-DD floor (idempotent — also the fix for a stale floor). |
| `/phase2` | Wizard: switch to the funded phase; reset the consistency log + fee anchor. |
| `/consistency` | Phase 2 daily-profit breakdown + consistency-rule status. |

**Trading control:** `/start`, `/stop`, `/resume` (clear today's soft kills), `/rearm` (re-arm soft kills),
`/setmaxpos <n>`, `/maxpos`, `/setdayroll <HH:MM>`, `/setwindow` (wizard).
**Reporting:** `/status`, `/equity` (full-history fee scan), `/positions`, `/pnl`, `/health`,
`/checksymbols`, `/news`.
**Pair control:** `/closepair` (wizard), `/resumepair`.
**Ops:** `/update` (wizard — deploy steps), `/emergency` (wizard — force close all), `/help`, `/cancel`.
**Optional (only if the mode toggle is kept):** `/mode [conservative|aggressive]`, `/setrisk <mode> <pct>`.

Wizards are `ConversationHandler`s with `allow_reentry=True`; `/cancel` aborts.

## Required alert messages (`msg_*`)
**Kill alerts** (account-wide): `msg_kill1_daily`, `msg_kill2_overall`, `msg_kill3_daily_cap` (P2+),
`msg_kill4_profit_target`, `msg_kill5_consistency` (P2); Phase 1 stage events: `msg_stage_reached`,
`msg_phase1_passed` (final stage → funded).
**Trade lifecycle:** `msg_trade_opened` (phase-aware: shows phase, lots, entry/sl/tp, risk $, target/RR,
and in Phase 1 the active stage + reward gap), `msg_position_closed` (real net P&L when `deal_pnl.found`,
else `(est.)`), `msg_signal_not_placed_preflight`, `msg_order_not_filled`.
**Blocked/skipped:** `msg_signal_blocked_p_halt`, `msg_signal_skipped_halted`, `msg_signal_suppressed`,
`msg_signal_skipped_max_pos`, `msg_signal_skipped_dedup`, `msg_signal_blocked_algo_disabled`,
`msg_baseline_missing`, `msg_geometry_reject`.
**System:** `msg_new_session_auto_resumed`, `msg_worker_offline`, `msg_worker_back_online`,
`msg_curfew_close`, `msg_news_pre_close`, `msg_internal_error`, `msg_contract_query_failed`.

> The kill alerts are this system's own halt notifications. Keep their wording self-contained and
> consistent (they are the system's audit trail of why a trade or the account stopped).

## Structured audit line (machine-readable footer on lifecycle alerts)
Every **Trade Opened / Position Closed / Kill** alert ends with one stable, parseable line so the alert
doubles as a machine-readable audit/integration record (useful for logging, dashboards, and any external
consumer of this system's public alerts). Keep the format fixed:
```
OPEN|pair=<TICKER>|dir=<LONG|SHORT>|entry=<px>|sl=<px>|tp=<px>|lots=<n>|phase=<1|2>
CLOSE|pair=<TICKER>|reason=<TP|SL|MANUAL|K1..K5|...>
KILL|k=<K1..K5|FORCE>|scope=<account|TICKER>
```
This is just complete self-reporting of the system's own actions — keep it generic and stable; do not tie
it to any external consumer. (Stability matters: downstream tooling parses it. A wording change to the
human-readable rows is fine; changing this line's field names/order is a breaking change.)
