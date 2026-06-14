# 00 — AGENT START HERE (read this first, in full)

You are an autonomous build agent. Your job: **build a brand-new standalone single-leg personal
trading system** in a **new repository**, using the existing `arbitrage-trading` repo **only as a
reference**. This folder (`docs/personal-leg/`) is your complete specification. Read every file listed
below before writing any code. Do not improvise structure or contracts — they are all specified here.

## The golden rules (violating any of these is a failure)

1. **Build in a NEW repo, separate from `arbitrage-trading`.** You only ever READ the reference repo;
   you never edit it. Target path is confirmed at **CHECKPOINT CP-0** below.
2. **TDD always.** For every pure-logic module, write the tests (from `08-test-plan.md`) FIRST, watch
   them fail, then implement until green. Never write logic before its test.
3. **The math is fixed.** `compute_personal_geometry` and `dollar_per_unit` must match
   `02-calculation-parity.md` exactly. The first geometry test pins exact numbers — do not "improve"
   the formula.
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
| 1 | `01-master-plan.md` | The "why", the 6 locked decisions, the native math, two-mode toggle, halts, architecture. |
| 2 | `02-calculation-parity.md` | The exact math + the first regression test (worked numbers). **Memorize this.** |
| 3 | `04-system-architecture.md` | The exact target repo layout, every file you will create, the process model, old→new mapping. |
| 4 | `05-data-contracts.md` | Exact schemas: 14-field webhook, ZMQ ticket, ZMQ REP queries, `personal_config.json`, env vars. |
| 5 | `06-build-tasks.md` | **Your runbook.** Numbered tasks T0→T14 in strict order. Execute them top to bottom. |
| 6 | `07-telegram-spec.md` | Every Telegram command + message format + currency rules. |
| 7 | `08-test-plan.md` | Every test case with exact expected values. |
| 8 | `09-deploy-runbook.md` | Demo deploy, MT5 connect, hosting, go-live gates. |

## Reference repo — files you may READ (never edit), with exact paths

Read a reference file only when the task that needs it tells you to (don't pre-read everything):

```
Kernel / math (copy verbatim, fix imports):
  layer2/strategy_common.py            dollar_per_unit, invert_signal
  layer2/phase2_strategy.py            the geometry you reverse onto personal
Pair registry / filters (copy + adapt):
  layer2/symbols.py + config/symbols.json
  layer1/news_filter.py, layer1/ff_calendar.py
Worker reference (re-implement, do not blind-copy — it's prop-coupled):
  layer3/_worker_core.py               MT5 connect+guard (~197), execute (~717), REP (~1480), fee anchor (~1090), deal_pnl (~1394)
  layer3/symbol_mapper.py              copy + adapt
  layer3/journal/                      copy the pipeline
Contract references (already extracted for you in 05-data-contracts.md):
  layer1/main.py:88                    L1 SignalPayload (9 fields)
  layer2/logic_core.py:1006            L2 SignalPayload (14 fields)
  layer2/logic_core.py:1548            ZMQ ticket schema
State / day-roll / currency:
  layer2/state.py                      _propfirm_day, propfirm_day_roll, _money, _fmt_price, _ccy_prefix
Telegram:
  layer2/telegram_handlers.py          message format + command registry (see 07)
```

## Checkpoint protocol — the ONLY times you stop

| Checkpoint | When | You present | You wait for |
|---|---|---|---|
| **CP-0** | Before any code (start of T0) | Confirm: target repo path, that you've read all 8 spec files. | Warren: target path (default suggestion in T0). |
| **CP-1** | After T11 — all code written, full test suite green | A short report: file tree, `pytest` output (all green), and a dry-run trace of one simulated signal → ticket. | Warren: the open numbers (mode %s, daily/overall DD %, `personal_baseline`), Firebase creds, Telegram bot token, VPS access, Receiver host. |
| **CP-2** | After T13 — deployed to demo | Confirmation both services are live, MT5 connected on the right account, one test signal flowed. | Warren: "soak it." |
| **CP-3** | After ≥7 demo trading days (T14) | Soak results: trades, P&L, any errors, journal entries. | Warren: go-live decision. |

Between checkpoints you run continuously. Now open `06-build-tasks.md` and start at **T0**.
