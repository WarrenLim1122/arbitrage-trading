# 06 — Build Tasks (execute T0 → T13 in strict order)

Your runbook for the **inverse-follower** personal system. Top to bottom, no skipping. Each task:
**Goal · Reference · Spec · Tests · Acceptance · Commit.** TDD: tests first. Commit + push after each.
Stop only at the CHECKPOINT tasks.

---

## ▣ T0 — CHECKPOINT CP-0: confirm feasibility + scaffold
- **STOP, confirm with Warren (BLOCKING):**
  1. He will run an **MTProto user session** for the prop reader (Bot API bots can't read another bot's
     messages). Get `api_id`, `api_hash`, the user account/phone for the one-time login, the shared
     `group_chat_id`, and the `prop_bot_username`/id. **If he won't run a user session, STOP — the
     follow-via-Telegram model is not buildable as-is; report back.**
  2. Target repo path (default `~/Coding Projects/personal-leg-system`); that you read `00`–`10`.
- `git init`; build the tree from `04`; `personal_config.example.json` (`05 §4`) → copy to live;
  `pyproject.toml` (incl. `telethon`); `.gitignore` (include `secrets/*.session`); short `README.md`.
- **Acceptance:** tree matches `04`; `pytest` collects 0, no import errors; first commit pushed.
- **Commit:** `chore: scaffold personal-leg-system (inverse follower) skeleton + config`
- **➡ After Warren confirms, T1–T10 run autonomously.**

## ▣ T1 — Port reuse modules
- **Ref:** `layer2/strategy_common.py` (`invert_signal`, `dollar_per_unit`), `layer2/symbols.py`,
  `config/symbols.json`, `layer3/symbol_mapper.py`, `layer3/journal/`.
- **Spec:** copy to new locations (`04`); fix imports only; `symbols` exposes the canonical pair set.
- **Tests:** `tests/test_imports.py` — import all; `symbols.load()` non-empty.
- **Commit:** `feat: port reused modules (strategy_common, symbols, symbol_mapper, journal)`

## ▣ T2 — `common/reconstruct.py` (TDD — the core math)
- **Ref:** `02-calculation-parity.md`; `layer2/phase1_strategy.py:171-172` + `phase2_strategy.py`.
- **Tests FIRST (`tests/test_reconstruction.py`, `08 §1`):** the §2 Phase-2 case
  (LONG, lots 12.96, sl 1.08300, tp 1.08554); Phase-1 case (prop_lots 1.00 → lots 0.20); prop LONG →
  personal SHORT; tiny-lots → `{"reject"}`.
- **Spec:** `reconstruct_personal(*, pair, prop_signal, prop_sl, prop_tp, prop_lots, phase, price_digits,
  phase_multipliers) -> dict|{"reject"}` per `02 §1`: `pers_signal=invert(prop_signal)`,
  `pers_lots=round(prop_lots×mult,2)`, `pers_sl=round(prop_tp,d)`, `pers_tp=round(prop_sl,d)`.
- **Commit:** `feat: hedge reconstruction (inverse mirror of the prop trade)`

## ▣ T3 — `receiver/parser.py` (TDD — prop alert → event dict)
- **Ref:** `05 §1` (structured line + keyword fallback); prop alert formats in `prop-leg/07`.
- **Tests FIRST (`tests/test_parser.py`, `08 §2`):** parse an `OPEN|...` line → open event with all fields;
  `CLOSE|pair=..|reason=..` → close event; `KILL|k=K2|scope=account` → kill event; keyword-only fallback
  text; a non-prop-sender message → ignored (None); a malformed line → ignored + logged.
- **Spec:** pure `parse_prop_message(text, sender, cfg) -> event|None`. Sender filter first; then try the
  structured line; then keyword fallback; extract a pair via the canonical registry when present.
- **Commit:** `feat: prop-alert parser (structured line + keyword fallback)`

## ▣ T4 — `receiver/zmq_client.py`
- **Spec:** `push_ticket(ticket)`; `push_close(pair)`; `query(req, timeout=3s)` with retry (3×, 3s);
  helpers `query_equity/positions/order_check/order_status/deal_pnl`. Ticket shape = `05 §2`.
- **Tests:** `tests/test_zmq_client.py` round-trip vs a fake REP; assert ticket shape + clean timeout.
- **Commit:** `feat: ZMQ client — PUSH ticket/close + REQ queries (3s timeout + retry)`

## ▣ T5 — Worker: connect + execute + queries + close
- **Ref:** `layer3/_worker_core.py` (`_connect_mt5`~197, `_execute_order`~717, `_get_filling_mode`,
  `_place_limit_order`, `_rep_loop`~1480 + builders, `_build_deal_pnl_reply`~1394, `_force_close_ticker`).
- **Spec:** `mt5_connect.py` self-launch (`initialize(path,timeout=120000)`, no login args), hard guard
  `account_info().login==MT5_LOGIN` else `SystemExit(1)`, cache `account_mode`. `execute.py` market +
  MARKET_CLOSED retry (bg thread) + LIMIT fallback, filling IOC→FOK→RETURN, force-close by pair.
  `queries.py` REP builders (`05 §3`) with server-tz windows + strict `deal_pnl` matching.
- **Tests:** filling order; ticket→request mapping; deal-window=now+1day; `deal_pnl` found=False path.
  Live MT5 → CP-2.
- **Commit:** `feat: worker — MT5 connect+guard, execute, REP queries, force-close`

## ▣ T6 — Worker: close watcher + journaling
- **Ref:** `_position_close_watcher`, `layer3/journal/journaling_worker.py:324`.
- **Spec:** detect close → immediate screenshot + deferred deal-history (backoff/queue); server-tz chart
  contract; currency badge = account_currency.
- **Tests:** record builder (currency badge). Pipeline integration → CP-2.
- **Commit:** `feat: worker — close watcher + journaling pipeline`

## ▣ T7 — `receiver/prop_reader.py` (MTProto user session) + `prop_follower.py` (TDD on routing)
- **Ref:** `10-prop-follower.md`; Telethon docs (events.NewMessage on the group).
- **Spec:** `prop_reader.py` — a Telethon **user client** (api_id/api_hash/session from config) joined to
  `group_chat_id`; on each new message call `parse_prop_message`; emit events to the follower.
  `prop_follower.py` — route events: **open** → `reconstruct_personal` (needs `price_digits` from a
  `query_equity` on the pair) → guard (`follow_enabled`, `active`, not halted, dedup by pair,
  `max_open_positions`, `order_check`) → `push_ticket`; **close** → `push_close(pair)` + update the
  by-pair position map; **kill** → apply `kill_action` (`10 §4`) → close + set halt → alert.
  `scripts/mtproto_login.py` creates the session once.
- **Tests (`tests/test_follower.py`, `08 §3`):** open event → correct ticket pushed (mocked zmq + equity);
  close event → close pushed for the right pair; K2 event → close-all + halt; non-prop sender ignored;
  `follow_enabled=false` → no-op; dedup (pair already open) → no second open.
- **Commit:** `feat: MTProto prop reader + event follower (open/close/kill routing)`

## ▣ T8 — `receiver/monitor.py` (reconcile, health, day-roll, optional DD)
- **Spec:** 30s loop: `query_equity`/`positions` (worker health alerts); detect a personal position that
  closed → send Position Closed alert (real net via `deal_pnl`, else `(est.)`); reconcile drift (a prop
  close missed → ensure the personal pair is flat); SGT day-roll housekeeping + auto-resume of a day-halt;
  if `secondary_dd.enabled`, evaluate the optional own-equity DD halt.
- **Tests:** close-detection alert path; day-roll reset; optional-DD trigger (when enabled). Mocked clock.
- **Commit:** `feat: receiver monitor — reconcile closes, health, day-roll, optional DD`

## ▣ T9 — `receiver/control_bot.py` + `messages.py` (TDD on formats)
- **Ref:** `07-telegram-spec.md`; `layer2/telegram_handlers.py` (format helpers, currency rules).
- **Spec:** personal Bot API bot with Warren's commands (`07`): `/status`, `/start`/`/stop`,
  `/follow on|off`, `/positions`, `/equity`, `/health`, `/closepair`, `/checksymbols`, `/update`, `/help`.
  `messages.py` all `msg_*` (━×12; currency from account_currency; sign before symbol; no `$+`/`$-`):
  `msg_hedge_opened`, `msg_position_closed` (real net else `(est.)`), `msg_prop_kill_action`,
  `msg_follow_paused`, `msg_worker_offline/online`, `msg_internal_error`.
- **Tests (`tests/test_messages.py`, `08 §4`):** SGD render (no `$`), `(est.)` path, prices no symbol.
- **Commit:** `feat: personal control bot + alert catalog (SGD-aware)`

## ▣ T10 — Wire entrypoints + full dry-run
- **Spec:** `receiver/main.py` starts the Telethon reader + follower + monitor + control bot + zmq.
  `worker/main.py` starts PULL+REP+close-watcher. `scripts/dry_run_prop_event.py` feeds a fake prop
  `OPEN|...` (and `CLOSE`, `KILL`) message through the parser→follower with a fake worker; assert the
  correct hedge ticket / close / halt.
- **Tests:** full suite green; capture the dry-run trace for CP-1.
- **Commit:** `feat: wire receiver+worker; end-to-end dry-run (prop event → hedge) green`

## ▣ T11 — CHECKPOINT CP-1: hand back
- **STOP.** Present: file tree, green `pytest`, and the dry-run trace (a fake prop OPEN → personal hedge
  ticket; a CLOSE → personal close; a K2 → close-all+halt).
- **Ask Warren:** confirm `phase_multipliers` (0.20/0.70); the **prop alert parse contract** (confirm the
  prop kit emits the `OPEN|CLOSE|KILL` structured line, or supply real prop alert samples to tune the
  keyword fallback); whether to enable the secondary DD halt; the MTProto creds + group id + prop bot id;
  personal MT5 login; Firebase creds; Receiver host. Fold into `personal_config.json`. **No deploy yet.**

## ▣ T12 — Deploy to DEMO → CHECKPOINT CP-2
- Per `09`: create the Telethon session (`scripts/mtproto_login.py`, one-time phone login); Receiver
  (systemd) + Worker (MT5 one-time connect, save password). Open :5555/:5556. Put both bots + the user
  account in the shared group. With the prop system also on demo, fire a real prop trade and confirm
  personal opens the inverse hedge, then closes with it.
- **CP-2:** account guard matches; prop OPEN → personal hedge fills → Trade Opened alert → prop CLOSE →
  personal closes → Position Closed (real net) → journal entry; a prop K-kill → personal closes+halts.

## ▣ T13 — DEMO SOAK ≥7 trading days → CHECKPOINT CP-3
- Run ≥7 trading days alongside the prop demo. Watch: every prop trade mirrored correctly (direction,
  lots = prop×mult, sl/tp swapped), closes reconciled, kills honored, no missed/duplicated mirrors,
  journal entries. **CP-3:** present results; Warren decides go-live.

## Done-definition
T0–T10 done = full suite green + correct dry-run (prop event → hedge). After T10 gated by Warren
(MTProto session, credentials, the live prop demo, soak time) via the checkpoints.
