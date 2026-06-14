# 04 — System Architecture & Target Repo Layout

Personal is a **Telegram-driven inverse follower** of the prop system. Two processes: a Linux **Receiver**
(reads the prop bot's alerts via MTProto, reconstructs + dispatches the hedge, control bot, reconciliation)
and a Windows **Worker** (executes on the personal MT5 account). **No webhook, no Pine, no news filter** on
the personal side.

## Process model
```
Shared Telegram group ── prop bot posts trade/close/kill alerts ──┐
                                                                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ RECEIVER  — Linux VPS, systemd                                                  │
│  prop_reader.py   MTProto USER session (Telethon) — reads ALL group messages   │
│       │           (a Bot API bot CANNOT read another bot's messages)            │
│       ▼                                                                         │
│  prop_follower.py parse prop event → route (open / close / halt)   [10]         │
│       ▼                                                                         │
│  reconstruct.py   inverse hedge (02)  → zmq_client PUSH ticket                  │
│  monitor.py       reconcile prop closes, worker health, optional 2ndary DD halt │
│  control_bot.py   personal Bot API bot: /status /stop /positions /equity ...    │
└──────────────── ZMQ PUSH :5555 / REQ :5556 ───────────────────────────────────┘
                                  │
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ WORKER  — Windows VPS (personal MT5 terminal)                                   │
│  mt5_connect (self-launch + hard account guard) | execute (retry/limit) | REP   │
│  close_watcher → journaling                                                     │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Target repo layout (`~/Coding Projects/personal-leg-system/`, greenfield)
```
personal-leg-system/
├── pyproject.toml          # fastapi(optional for health), pyzmq, python-telegram-bot (control bot),
│                           # telethon (MTProto reader), MetaTrader5 (worker), firebase-admin, pytest
├── README.md  .gitignore
├── config/
│   ├── personal_config.json / .example.json   # schema 05 §4
│   └── symbols.json                            # copied from reference
├── common/
│   ├── strategy_common.py    # COPIED (invert_signal; dollar_per_unit kept for any worker-side use)
│   ├── reconstruct.py        # NEW: reconstruct_personal() — the mirror (02)
│   └── symbols.py            # adapted registry loader (canonical pair set)
├── receiver/
│   ├── prop_reader.py        # Telethon user-session client; yields prop-bot messages
│   ├── prop_follower.py      # parse + route prop events (open/close/halt) — 10
│   ├── parser.py             # pure: prop alert text/structured-line → event dict (10 §3)
│   ├── zmq_client.py         # PUSH ticket, REQ queries
│   ├── monitor.py            # reconcile closes, worker health, optional DD halt, day-roll
│   ├── control_bot.py        # personal Bot API bot (Warren's commands)
│   ├── messages.py           # personal msg_* builders
│   └── state.py              # config IO, currency/price helpers, day-roll, position map (by pair)
├── worker/
│   ├── main.py  mt5_connect.py  execute.py  queries.py  close_watcher.py
│   ├── symbol_mapper.py  journal/
├── secrets/                  # gitignored: telethon .session, firebase-service-account.json, .env
├── scripts/
│   ├── mtproto_login.py      # one-time: create the Telethon user session
│   └── dry_run_prop_event.py # feed a fake prop alert → assert correct hedge ticket
└── tests/
    ├── test_reconstruction.py  test_parser.py  test_follower.py
    ├── test_zmq_client.py  test_messages.py  test_dayroll.py
```

## Reference → new mapping
| Reference | New | Change |
|---|---|---|
| `layer0/*.pine`, `layer1/*` (signal+gatekeeper) | — | **dropped** — personal has no signal; input is the prop's Telegram alerts |
| `layer2/logic_core` geometry/gate chain | `receiver/prop_follower.py` + `common/reconstruct.py` | replaced by event-follow + mirror reconstruction |
| `pers_*` relationship in `phase1/2_strategy.py` | `common/reconstruct.py` | the only math personal keeps (02) |
| `layer2/strategy_common.py` | `common/strategy_common.py` | `invert_signal` verbatim |
| `layer2/state.py` | `receiver/state.py` | day-roll + currency helpers + a by-pair position map; drop prop/phase/baseline |
| `layer2/telegram_handlers.py` | `receiver/control_bot.py` + `messages.py` | personal control + alerts only |
| `layer3/_worker_core.py` | `worker/*.py` | single personal account |
| `layer3/symbol_mapper.py`, `layer3/journal/` | `worker/*` | copy |
| (new) | `receiver/prop_reader.py` + `parser.py` | the MTProto reader + prop-alert parser |

## What is dropped entirely
Own Pine/webhook, the `/signal` endpoint, the news filter + `ff_calendar`, geometry-from-signal, the
native baseline sizing + two-mode toggle (all from the abandoned independent design), and any prop-firm
challenge logic (that lives only in the prop system).
