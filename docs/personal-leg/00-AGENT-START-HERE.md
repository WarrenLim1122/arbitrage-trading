# 00 — AGENT START HERE (read this first, in full)

You are an autonomous build agent. **You are running INSIDE the `arbitrage-trading` repo** — that repo
is your **read-only reference**. Your job: **build the personal trading system** in a **separate sibling
folder** (its own new git repo), at `~/Coding Projects/personal-leg-system/` (name confirmed at CP-0).
This folder (`docs/personal-leg/`) is your complete specification. Read every file listed below before
writing any code. Do not improvise structure or contracts — they are all specified here.

**What this system is (read carefully — it is NOT a standalone signal trader):** the personal system has
**no signal of its own**. It is the **inverse follower** of a *separate* prop system. It reads the prop
bot's Telegram alerts (Trade Opened / Position Closed / Kill K1–K5) and mirrors each event on the personal
MT5 account as the **exact inverse hedge** (`pers_lots = prop_lots × phase_multiplier`, swapped SL/TP,
opposite direction). This reproduces the original coupled hedge while keeping the prop system unaware of
personal. Full model: `01-master-plan.md`.

**⚠️ Hard feasibility constraint (CP-0):** Telegram Bot API bots cannot read other bots' messages, so the
prop-alert reader **must** be a Telegram **user client (MTProto / Telethon)** with a user session
(`api_id`+`api_hash`+phone login). If Warren won't provide that, STOP at CP-0 and report — the model is
not buildable without it. Personal's own *control* bot can be a normal Bot API bot.

**Read-from-here, write-over-there:** you READ this `arbitrage-trading` repo (the kit + the reference
source files) and you WRITE only to the new sibling folder. Both are on the same machine; `~/Coding
Projects/` is writable (verified). You never edit, commit to, or delete anything in `arbitrage-trading`.

## The golden rules (violating any of these is a failure)

1. **Build in a NEW repo at a SIBLING folder** (`~/Coding Projects/personal-leg-system/`), separate from
   `arbitrage-trading`. You only ever READ the reference repo; you never edit it. Path confirmed at **CP-0**.
2. **TDD always.** For every pure-logic module, write the tests (from `08-test-plan.md`) FIRST, watch
   them fail, then implement until green. Never write logic before its test.
3. **The math is fixed.** `reconstruct_personal` must match `02-calculation-parity.md` exactly
   (`pers_dir = inverse(prop_dir)`, `pers_lots = round(prop_lots × phase_mult, 2)`, `pers_sl = prop_tp`,
   `pers_tp = prop_sl`). The first reconstruction test pins exact numbers — do not "improve" it.
4. **Commit + push after every task** (each task below ends with a commit message). Small, frequent commits.
5. **Never delete/move/overwrite files you didn't create.** Copy reference modules into the new repo;
   do not modify the originals.
6. **Stop ONLY at the labelled CHECKPOINTS.** Everything else runs autonomously, start to finish.
   Do not stop to ask permission for routine coding decisions — the spec already made them.
7. **If a spec is genuinely ambiguous,** pick the option that most faithfully mirrors the reference
   system's behavior (the whole design principle is "same formulas, single leg"), note your choice in
   the commit body, and keep going. Do not stall.

## Read these before coding (in order)

| Order | File | What you get |
|---|---|---|
| 1 | `01-master-plan.md` | The model (inverse follower), the 6 locked decisions, the MTProto constraint, architecture. |
| 2 | `02-calculation-parity.md` | The reconstruction math + the first regression test (worked numbers). **Memorize this.** |
| 3 | `04-system-architecture.md` | The exact target repo layout, every file you create, the process model, reference→new map. |
| 4 | `05-data-contracts.md` | Exact schemas: prop-alert parse contract, ZMQ ticket, REP queries, `personal_config.json`, env. |
| 5 | `06-build-tasks.md` | **Your runbook.** Numbered tasks T0→T13 in strict order. Execute them top to bottom. |
| 6 | `07-telegram-spec.md` | Personal control commands + message format + currency rules. |
| 7 | `08-test-plan.md` | Every test case with exact expected values. |
| 8 | `09-deploy-runbook.md` | MTProto session setup, MT5 connect, hosting, go-live gates. |
| 9 | `10-prop-follower.md` | **The core** — how personal reads + routes the prop bot's open/close/kill alerts. |

(`10-prop-halt-listener.md` is a superseded redirect — ignore it; `10-prop-follower.md` replaces it.)

## Reference repo — files you may READ (never edit), with exact paths

Read a reference file only when the task that needs it tells you to (don't pre-read everything):

```
Math (copy / port):
  layer2/strategy_common.py            invert_signal (the reconstruction core); dollar_per_unit (worker)
  layer2/phase1_strategy.py:171-172    proves pers_sl=prop_tp, pers_tp=prop_sl
  layer2/phase2_strategy.py            proves the same + pers_lots=prop_lots×ratio
Pair registry (copy + adapt):
  layer2/symbols.py + config/symbols.json
Worker reference (re-implement, single personal account):
  layer3/_worker_core.py               MT5 connect+guard (~197), execute (~717), REP (~1480), deal_pnl (~1394)
  layer3/symbol_mapper.py              copy + adapt
  layer3/journal/                      copy the pipeline
ZMQ ticket reference (extracted in 05):
  layer2/logic_core.py:1548            ticket schema
State / day-roll / currency:
  layer2/state.py                      _propfirm_day, propfirm_day_roll, _money, _fmt_price, _ccy_prefix
Telegram format:
  layer2/telegram_handlers.py          message format + helpers (see 07)
```
> There is **no** webhook/Pine/news-filter to port on the personal side — its input is the prop's
> Telegram alerts (`10`), not a TradingView signal.

## Checkpoint protocol — the ONLY times you stop

| Checkpoint | When | You present | You wait for |
|---|---|---|---|
| **CP-0** | Before any code (start of T0) | Confirm target repo path; **confirm Warren will run an MTProto user session** (the BLOCKING feasibility gate); that you read all spec files. | Warren: MTProto `api_id`+`api_hash`+account, shared `group_chat_id`, `prop_bot_username`, target path. |
| **CP-1** | After T10 — all code written, full suite green | File tree, `pytest` (all green), and a dry-run trace (fake prop OPEN → personal hedge ticket; CLOSE → close; K2 → close-all+halt). | Warren: `phase_multipliers`, the prop alert parse contract, secondary-DD on/off, Firebase creds, Telegram control-bot token, personal MT5 login, Receiver host. |
| **CP-2** | After T12 — deployed to demo | Both services live, MT5 on the right account, a real prop demo trade mirrored. | Warren: "soak it." |
| **CP-3** | After ≥7 demo trading days (T13) | Soak results: mirrored trades, P&L, missed/dupe mirrors, journal entries. | Warren: go-live decision. |

Between checkpoints you run continuously. Now open `06-build-tasks.md` and start at **T0**.
