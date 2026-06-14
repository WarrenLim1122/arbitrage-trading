# 07 — Telegram Spec (personal control bot + the prop reader)

Two distinct Telegram pieces on the personal side:

1. **Prop reader — MTProto USER client (Telethon), not a bot.** Read-only; it consumes the prop bot's
   alerts from the shared group. Bot API bots can't read other bots' messages, so this MUST be a user
   session (`api_id`+`api_hash`+phone login). It posts nothing. Parsing contract: `05 §1` + `10 §3`.
2. **Control bot — a normal Bot API bot** for Warren's commands + personal's own alerts.

## Control-bot commands
| Command | Behavior |
|---|---|
| `/status` | follow on/off, active/halt state, open personal positions, last prop event seen, last action. |
| `/start` / `/stop` | master on/off for acting on prop events (positions untouched by `/stop`). |
| `/follow on` / `/follow off` | pause/resume following (alias of start/stop for clarity). |
| `/positions` | open personal positions with live P&L. |
| `/equity` | balance/equity/P&L + all-in trading fee (full-history fee scan; `want_fee=true`). |
| `/health` | worker reachable? MT5 connected? account match? MTProto reader connected? |
| `/closepair` | manually close a personal pair. |
| `/checksymbols` | per-broker SUPPORTED/FOUND/MISSING (personal broker). |
| `/prophalt` | show the last prop kill seen + the action taken. |
| `/update` | deploy helper (prints redeploy steps — `09`). |
| `/help`, `/cancel` | help; abort a wizard. |

(No `/phase*`, `/changepropfirm`, `/setbaseline`, `/mode`, `/setrisk` — personal doesn't size itself or
run a challenge; those belong to the prop system.)

## Currency rules (there's a test)
- **Render money in the personal MT5's reported currency** (`account_currency` from `equity`) — currently
  SGD; auto-detected, no hardcoded symbol. `_ccy_prefix(ccy)` → `"$"` if USD else `"<ISO> "`.
- **Sign before symbol** in alerts (`-SGD 12.50`, `+SGD 30.00`). **Forex prices carry no symbol**
  (`fmt_price`). Audit invariant: no `$+`/`$-`; no `$` in a personal (SGD) alert.

## Alert messages (`msg_*`)
`msg_hedge_opened` (pair, personal direction, lots = prop_lots×mult, sl/tp, the prop event it mirrors),
`msg_position_closed` (real net via `deal_pnl`, else `(est.)`), `msg_prop_kill_action`
(which prop kill + what personal did), `msg_follow_paused`, `msg_signal_not_placed_preflight`,
`msg_order_not_filled`, `msg_worker_offline`, `msg_worker_back_online`, `msg_reader_disconnected`
(MTProto link down — important: if the reader drops, personal stops mirroring; alert loudly),
`msg_internal_error`, `msg_contract_query_failed`.

> **Reader-health is critical:** if the MTProto reader disconnects, personal silently stops hedging.
> The monitor must detect a stale reader (no messages / disconnect) and fire `msg_reader_disconnected`.
