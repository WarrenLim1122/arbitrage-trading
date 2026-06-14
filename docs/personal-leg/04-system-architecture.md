# 04 — System Architecture & Target Repo Layout

The standalone system is **two processes** (the absolute minimum: a public HTTPS receiver + a Windows
MT5 worker). It collapses the reference repo's Layers 0/1/2 into one **Receiver** and keeps one
**Worker** (the personal one). All prop/hedge machinery is removed.

## Process model

```
TradingView (15m chart per pair)
  └─ Layer 0 Pine indicator (REUSED, unchanged on TV) ──HTTPS POST──┐
                                                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ RECEIVER  — Linux VPS, systemd service, public TLS (nginx), one process       │
│   FastAPI /signal  →  gate chain  →  geometry  →  ZMQ PUSH ticket             │
│   + Telegram bot (commands + alerts)                                          │
│   + equity monitor thread (poll worker, halts, close detect, day roll)        │
└───────────────── ZMQ PUSH :5555 (ticket) / REQ :5556 (query) ────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ WORKER  — Windows VPS, PowerShell process, local MT5 terminal                 │
│   PULL :5555 execute order   |   REP :5556 answer queries                     │
│   + MT5 self-launch + hard account guard                                      │
│   + position-close watcher → journaling pipeline (Firestore)                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

Why one receiver (not L1+L2 split): a single account has no second leg to coordinate, so the internal
L1→L2 HTTP hop and the dual-leg pre-flight orphan problem both disappear. News/time filtering folds
into the same process.

## Target repo layout (create exactly this)

New repo (greenfield — no EPERM limits; that constraint was specific to `arbitrage-trading`):

```
personal-leg-system/                    # name/path confirmed at CP-0
├── pyproject.toml                       # deps: fastapi, uvicorn, pydantic, pyzmq, python-telegram-bot,
│                                        #       requests, MetaTrader5 (worker only), firebase-admin, pytest
├── README.md                            # short: what it is, how to run each service, link back to this kit
├── .gitignore                           # secrets/, config/*_cache_*.json, config/fee_anchor_*.json, *.lock, __pycache__
├── config/
│   ├── personal_config.json             # LIVE config (schema in 05). Git-tracked; runtime-edited on VPS.
│   ├── personal_config.example.json     # seed template, all placeholders
│   └── symbols.json                     # copied from reference config/symbols.json
├── common/
│   ├── __init__.py
│   ├── strategy_common.py               # COPIED VERBATIM from reference (dollar_per_unit, invert_signal)
│   ├── geometry.py                      # NEW: compute_personal_geometry (the kernel + native math)
│   └── symbols.py                       # adapted from reference layer2/symbols.py (registry loader)
├── receiver/
│   ├── __init__.py
│   ├── main.py                          # FastAPI app, /signal endpoint, gate chain, startup wiring
│   ├── state.py                         # config load/save, locks, day-roll (SGT), currency helpers
│   ├── halts.py                         # daily + overall DD evaluation (pure)
│   ├── news_filter.py                   # copied/adapted from reference layer1/news_filter.py
│   ├── ff_calendar.py                   # copied from reference layer1/ff_calendar.py
│   ├── zmq_client.py                    # PUSH ticket, REQ queries (equity/order_check/order_status/...)
│   ├── monitor.py                       # equity-monitor thread: poll, halts, close detect, day roll, auto-resume
│   ├── telegram_bot.py                  # command handlers + bot wiring
│   └── messages.py                      # ALL msg_* string builders (no strings inline elsewhere)
├── worker/
│   ├── __init__.py
│   ├── main.py                          # entrypoint: threads (PULL, REP, SGT scheduler, close watcher)
│   ├── mt5_connect.py                   # self-launch + hard account guard (login match → else SystemExit)
│   ├── execute.py                       # _execute_order: market + retry + limit fallback, filling-mode detect
│   ├── queries.py                       # REP builders (equity, positions, order_status, order_check, deal_pnl, account_mode, checksymbols)
│   ├── close_watcher.py                 # detect TP/SL/manual close → fire journaling
│   ├── symbol_mapper.py                 # copied/adapted from reference layer3/symbol_mapper.py
│   └── journal/                         # copied from reference layer3/journal/ (firebase_journal, screenshot, rr_chart, ...)
├── secrets/                             # gitignored: firebase-service-account.json, .env
├── scripts/
│   └── dry_run_signal.py                # send a fake webhook locally for the CP-1 trace
└── tests/
    ├── test_geometry.py                 # 08 §1
    ├── test_halts.py                    # 08 §2
    ├── test_dayroll.py                  # 08 §3
    ├── test_webhook_validation.py       # 08 §4
    ├── test_gate_chain.py               # 08 §5 (mocked ZMQ)
    └── test_messages.py                 # 08 §6 (format + currency rules)
```

## Old → new mapping (what each reference piece becomes)

| Reference (arbitrage-trading) | New repo | Change |
|---|---|---|
| `layer0/*.pine` | (stays on TradingView) | unchanged — same 14-field webhook |
| `layer1/main.py` (gatekeeper) | folded into `receiver/main.py` gate chain | news/time gate kept; internal-HTTP hop removed |
| `layer1/news_filter.py`, `ff_calendar.py` | `receiver/news_filter.py`, `ff_calendar.py` | copy |
| `layer2/logic_core.py` (orchestration) | `receiver/main.py` + `receiver/monitor.py` | single leg; no phase branch; no dual-leg preflight |
| `layer2/phase1_strategy.py`, `phase2_strategy.py` | `common/geometry.py` | replaced by ONE native function (02-calculation-parity) |
| `layer2/strategy_common.py` | `common/strategy_common.py` | verbatim |
| `layer2/state.py` | `receiver/state.py` | keep day-roll + currency helpers; drop propfirm/phase/consistency |
| `layer2/telegram_handlers.py` | `receiver/telegram_bot.py` + `receiver/messages.py` | drop prop/phase commands & kill msgs; keep the rest |
| `layer2/symbols.py` + `config/symbols.json` | `common/symbols.py` + `config/symbols.json` | copy |
| `layer3/_worker_core.py` | `worker/*.py` (split) | re-implement single-account; drop static-DD prop guard (replaced by receiver halts) |
| `layer3/worker_personal.py` | `worker/main.py` | the only worker; no prop shim |
| `layer3/symbol_mapper.py`, `layer3/journal/` | `worker/symbol_mapper.py`, `worker/journal/` | copy |
| `config/risk_params.json`, `propfirm_config.json`, `phase_config.json`, `consistency_log.json` | `config/personal_config.json` | one consolidated config |

## What is deleted entirely (do not port)
Prop worker · inverse-direction leg · Phase 1/2 strategies · stage ladder & ratchet · consistency log
& K5 · daily-profit-cap K3 · profit-target K4 · `baseline_equity`-as-prop-anchor · dual-leg pre-flight ·
`/phase1` `/phase2` `/changepropfirm` `/consistency` `/propfirm` commands · `reset_fee_anchor` on two
workers (keep it for the single worker).
