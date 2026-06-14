# 04 — System Architecture & Target Repo Layout

Two processes: a public HTTPS **Receiver** + a Windows MT5 **Worker**. Single account, single signal
source. Naming rule from `00` applies to every file below.

## Process model
```
TradingView (15m chart per pair)
  └─ this system's Pine indicators (breakout-fade / div-fade / NW-fade) ──HTTPS POST──┐
                                                                                       ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ RECEIVER  — Linux VPS, systemd, public TLS (nginx), one process                       │
│   FastAPI /signal → gate chain → PHASE-AWARE geometry → ZMQ PUSH ticket               │
│   + Telegram bot (challenge wizards: /changepropfirm /phase1 /phase2 + alerts)        │
│   + equity monitor (poll worker, K1–K5, day-roll, auto-resume, consistency-log lock)  │
└──────────────────── ZMQ PUSH :5555 / REQ :5556 ──────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ WORKER  — Windows VPS, PowerShell, local MT5 terminal                                 │
│   PULL execute  |  REP queries  |  local static-DD guard  |  close-watcher → journal  │
│   + MT5 self-launch + hard account guard                                              │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Target repo layout (`~/Coding Projects/prop-leg-system/`, greenfield)
```
prop-leg-system/
├── pyproject.toml            # fastapi, uvicorn, pydantic, pyzmq, python-telegram-bot, requests,
│                             # MetaTrader5 (worker), firebase-admin, pytest
├── README.md  .gitignore
├── config/
│   ├── account_config.json           # LIVE config (schema in 05 §4); git-tracked, runtime-edited on VPS
│   ├── account_config.example.json   # seed template, placeholders
│   └── symbols.json                  # copied from reference
├── common/
│   ├── strategy_common.py            # COPIED VERBATIM (dollar_per_unit, invert_signal)
│   ├── phase1.py                     # ported phase1_strategy (single-account)
│   ├── phase2.py                     # ported phase2_strategy (single-account)
│   ├── kills.py                      # K1–K5 evaluation + buffers (pure)
│   └── symbols.py                    # adapted registry loader
├── receiver/
│   ├── main.py                       # FastAPI /signal, gate chain, startup wiring
│   ├── state.py                      # config IO, day-roll (SGT), currency helpers, consistency log
│   ├── zmq_client.py                 # PUSH ticket, REQ queries
│   ├── monitor.py                    # equity monitor: poll, K1–K5, day-roll, auto-resume, consistency lock
│   ├── telegram_bot.py               # commands + challenge wizards
│   ├── messages.py                   # ALL msg_* builders (incl. kill alerts)
│   ├── news_filter.py  ff_calendar.py# copied/adapted
│   └── wizards.py                    # /changepropfirm, /phase1, /phase2 ConversationHandlers
├── worker/
│   ├── main.py                       # threads: PULL, REP, static-DD guard, SGT scheduler, close-watcher
│   ├── mt5_connect.py                # self-launch + hard account guard
│   ├── execute.py                    # market + retry + limit fallback, filling-mode detect, force-close
│   ├── queries.py                    # REP builders (equity/positions/order_status/order_check/deal_pnl/account_mode/checksymbols/reset_fee_anchor)
│   ├── static_dd_guard.py            # local overall-DD backstop
│   ├── close_watcher.py              # close detect → journaling
│   ├── symbol_mapper.py  journal/    # copied/adapted
├── secrets/                          # gitignored: firebase-service-account.json, .env
├── scripts/dry_run_signal.py
└── tests/
    ├── test_phase1.py  test_phase2.py  test_kills.py  test_stages.py  test_buffers.py
    ├── test_dayroll.py  test_webhook_validation.py  test_gate_chain.py  test_messages.py
```

## Reference → new mapping
| Reference | New | Change |
|---|---|---|
| `layer0/*.pine` (detection) | this system's Pine (see `10`) | adapt detection; emit this system's direction/levels |
| `layer1/main.py` + `layer2/logic_core.py` | `receiver/main.py` + `receiver/monitor.py` | single-account; no internal HTTP hop; no dual-leg pre-flight |
| `layer2/phase1_strategy.py` | `common/phase1.py` | drop `pers_*`/`pers_ratio`; keep stage-ladder/moving-TP |
| `layer2/phase2_strategy.py` | `common/phase2.py` | drop `pers_*`/`phase_ratio`; keep the box |
| kills in `logic_core._run_equity_check` + `evaluate_kills` | `common/kills.py` | extract pure; account-wide |
| `layer2/strategy_common.py` | `common/strategy_common.py` | verbatim |
| `layer2/state.py` | `receiver/state.py` | keep buffers, day-roll, currency, consistency log; drop second-leg fields |
| `layer2/telegram_handlers.py` | `receiver/telegram_bot.py` + `messages.py` + `wizards.py` | keep challenge commands + kill alerts; single-account |
| `layer3/_worker_core.py` | `worker/*.py` | single-account; keep static-DD guard + journaling |
| `config/propfirm_config.json` + `phase_config.json` | `config/account_config.json` | consolidated (12 propfirm fields + phase block) |

## Currency
Auto-detected from the trading account (`account_currency` from the `equity` reply). Render all money in
that currency via the ported `_money`/`_ccy_prefix` helpers; forex prices carry no symbol. No hardcoded `$`.

## What is NOT ported (single-account)
Any second leg, the phase multiplier (`pers_ratio`/`phase_ratio`), dual-leg pre-flight, all `pers_*`
outputs, and every reference to another account/system. Drop them while porting.
