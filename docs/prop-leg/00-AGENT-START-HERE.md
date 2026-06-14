# 00 — AGENT START HERE (read this first, in full)

You are an autonomous build agent. **You are running INSIDE the `arbitrage-trading` repo** — that repo
is your **read-only reference**. Your job: build a **standalone prop-firm-challenge trading system** in a
**separate sibling folder** (its own new git repo) at `~/Coding Projects/prop-leg-system/` (name
confirmed at CP-0). This folder (`docs/prop-leg/`) is your complete specification.

**Read-from-here, write-over-there:** you READ this repo (the kit + the reference source files) and you
WRITE only to the new sibling folder. Both are on the same machine; `~/Coding Projects/` is writable.
You never edit, commit to, or delete anything in `arbitrage-trading`. The original system logic is
**frozen** — reference it, never modify it.

## What this system is
A self-contained automated trading system that takes its own signal, sizes every trade off a fixed
**baseline** risk anchor, runs a **two-phase prop-firm challenge** (an evaluation phase with a stage
ladder, then a funded phase with a consistency rule), and enforces five hard risk kills (K1–K5).
Currency is auto-detected from the trading account. Two processes: a Linux **Receiver** + a Windows
**Worker**.

## 🔒 NAMING RULE — non-negotiable, applies to ALL built artifacts
This is a self-contained system that trades **on its own**. In **all** code, comments, Pine scripts,
config, logs, and Telegram messages you write:
- **Never** reference any other account, system, or leg.
- **Never** use the words *personal, inverse, mirror, hedge, flip, opposite,* or "the other account."
- Describe every behavior in this system's **own absolute terms** (e.g. a SHORT with its stop above
  entry and target below — described as a *breakout-fade* setup, never as "the inverse of X").
This rule exists so the system reads as a standalone product. Honour it everywhere.

## The golden rules (violating any is a failure)
1. **Build in the NEW sibling repo** `~/Coding Projects/prop-leg-system/`. Reference repo is read-only.
2. **TDD always.** Write each task's tests (`08-test-plan.md`) FIRST, watch fail, implement to green.
3. **The math is fixed.** Geometry (Phase 1 + Phase 2) and kills K1–K5 must match `02-calculation-spec.md`
   exactly; the regression tests pin the numbers. Do not "improve" the formulas.
4. **Commit + push the new repo after every task** (each task gives a commit message).
5. **Never delete/move/overwrite files you didn't create.** Copy reference modules into the new repo.
6. **Stop ONLY at the labelled CHECKPOINTS** (CP-0…CP-3). Everything else runs autonomously.
7. **If a spec is genuinely ambiguous,** mirror the reference system's prop-side behavior, note the
   choice in the commit body, and keep going. Do not stall.

## Read these before coding (in order)
| # | File | What you get |
|---|---|---|
| 01 | `01-master-plan.md` | The decisions, the two-phase challenge, kills, sizing anchor, architecture. |
| 02 | `02-calculation-spec.md` | Exact geometry (P1 stage-ladder + P2 box) + kills K1–K5 + buffers, worked numbers. |
| 04 | `04-system-architecture.md` | Exact target repo layout, process model, reference→new mapping. |
| 05 | `05-data-contracts.md` | Schemas: 14-field webhook, ZMQ ticket, REP queries, `account_config.json`, env. |
| 06 | `06-build-tasks.md` | **Your runbook** — tasks T0→T14 in strict order. |
| 07 | `07-telegram-spec.md` | Commands (incl. challenge wizards) + message formats + currency rules. |
| 08 | `08-test-plan.md` | Every test case with exact expected values. |
| 09 | `09-deploy-runbook.md` | Demo deploy, MT5 connect, hosting, go-live gates. |
| 10 | `10-signal-engine-pine.md` | The system's own Pine indicators (breakout-fade / divergence-fade) + the webhook contract. |

## Reference files you may READ (never edit), exact paths
```
Signal engine (adapt the detection; emit THIS system's direction/levels — see 10):
  layer0/1D-15m Breakout INDICATOR.pine          (breakout detection + payload emission)
  layer0/Flipped RSI Divergence Indicator.pine   (RSI-divergence detection)
  layer0/Nadaraya-Watson Webhook INDICATOR.pine  (NW band detection)
Geometry + challenge logic (PORT near-verbatim, single-account):
  layer2/phase1_strategy.py        stage ladder, fixed-lot moving-TP, evaluate_kills
  layer2/phase2_strategy.py        fixed-risk box geometry
  layer2/strategy_common.py        dollar_per_unit, invert_signal
  layer2/state.py                  _apply_buffers (line 320), day-roll, _money/_fmt_price, consistency log
  layer2/logic_core.py             gate chain + _run_equity_check (kills, line ~890), monitor
Worker reference (re-implement single-account):
  layer3/_worker_core.py           MT5 connect+guard, execute, REP, static-DD guard, journaling
  layer3/symbol_mapper.py, layer3/journal/
Telegram:
  layer2/telegram_handlers.py      message format, kill alerts, /changepropfirm /phase1 /phase2 wizards
Pair registry:
  layer2/symbols.py + config/symbols.json
Reference docs (skim for context): docs/reference/calculations.md, execution.md, messages.md, deployment.md
```
> When you port from the reference, **strip every personal/second-leg reference** as you go (drop all
> `pers_*` outputs, the `phase_ratio`/`pers_ratio` multiplier, the dual-leg pre-flight). This system is
> single-account.

## Checkpoint protocol — the ONLY times you stop
| CP | When | You present | You wait for |
|---|---|---|---|
| **CP-0** | Before any code (T0) | Confirm target repo path + that you read all spec files. | Warren: path (default below). |
| **CP-1** | After T11 — all code written, full suite green | File tree, `pytest` output (green), a dry-run trace of one simulated signal → ticket, and a kills-fire simulation. | Warren: baseline, challenge limits (target/DD/consistency/profit-days/first-reward), Telegram token, Firebase creds, MT5 login, Receiver host. |
| **CP-2** | After T13 — deployed to demo | Both services live, MT5 on the right account, one test signal flowed, one kill simulated. | Warren: "soak it." |
| **CP-3** | After ≥7 demo trading days | Soak results. | Warren: go-live decision. |

Now open `06-build-tasks.md` and start at **T0**.
