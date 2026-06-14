# Knowledge Base — the "brain"

This folder is the **authoritative reference layer** for the arbitrage-trading engine.
It exists so a session can answer "where do I change X?" and "how does Y work?" **without
re-reading every code file**. Consult it first, then open the exact file:line it points to.

> **Maintenance rule (do not skip):** whenever you change code, update the matching KB
> page in the **same** session. A stale brain is worse than none. Each page lists the
> source files it summarizes — if you touch one of those, re-verify the page.

## Map

| Page | Answers | Primary source files |
|---|---|---|
| [architecture.md](architecture.md) | Layers, data flow, ZMQ/HTTP wiring, threads, VPS map, config files | `layer1/main.py`, `layer2/logic_core.py`, `layer2/state.py`, `config/*.json` |
| [calculations.md](calculations.md) | Risk math, lot sizing, Phase 1 vs Phase 2 SL/TP geometry, stages, kills K1–K5, buffers, day boundary | `layer2/phase1_strategy.py`, `layer2/phase2_strategy.py`, `layer2/strategy_common.py`, `layer2/state.py`, `layer2/logic_core.py::_run_equity_check` |
| [messages.md](messages.md) | Telegram `msg_*()` catalog, formatting standard (`_MSG_SEP`/`_cmd_*`), currency rules, command registry | `layer2/telegram_handlers.py` |
| [execution.md](execution.md) | Layer 3 worker: MT5 connect, symbol mapper, order execution, fee anchor, deal P&L, journaling, ZMQ protocol | `layer3/_worker_core.py`, `layer3/symbol_mapper.py`, `layer3/journal/*` |
| [deployment.md](deployment.md) | `/update` subcommands, worker-restart gotcha, deploy gates, where live config lives | `CLAUDE.md`, `TECHNICAL.md §Deploying`, `layer2/telegram_handlers.py::_cmd_update` |

## How this relates to the other docs

- **`CLAUDE.md`** — operational guide + current session state. Read for "what's pending / deployed".
- **`TECHNICAL.md`** — long-form reference (risk math derivations, Pine engine, MT5 gotchas,
  deploy gates, go-live checklist). The KB pages **link into** TECHNICAL.md rather than
  duplicating it. When a KB page and TECHNICAL.md disagree, the **code wins** — fix both.
- **`docs/MT5_VPS_Connection_Postmortem.md`** — the multi-week MT5 connection debugging story.
- **`docs/SESSION_HANDOFF.md`** — in-flight delta for the next session.

## Ground truth hierarchy

1. **The code** (always wins).
2. The KB + TECHNICAL.md (must be kept in sync with code).
3. CLAUDE.md Current State (session-scoped; ages fast).

Single sources of truth worth memorizing:
- **Symbols:** `config/symbols.json` (canonical = TradingView names). 33 today.
- **Risk anchor:** `baseline_equity` in `config/propfirm_config.json` — drives sizing AND all kills.
- **Risk %:** `prop_risk_pct = 0.01` (Phase 2 sizing) and `phase_multipliers {1:0.20, 2:0.70}` in `config/risk_params.json`.
- **All Telegram text:** `msg_*()` functions in `layer2/telegram_handlers.py` (logic_core is pure orchestration).
