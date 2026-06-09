# Section 04 — AI / Agent Outcomes: Trade Execution Engine (TEE)

---

## Core Pain Point Solved

Manual and semi-automated trading systems suffer from three compounding problems:

1. **Execution latency** — human reaction to signals introduces slippage and missed entries.
2. **Inconsistent risk discipline** — position sizing, drawdown limits, and kill conditions are routinely overridden under emotional pressure.
3. **Dual-account coordination** — simultaneously managing a proprietary trading firm account (directional) and a personal hedge account (inverse) is operationally impossible without automation.

The project solves all three by replacing the human execution loop with a fully autonomous, multi-layer agent system that processes signals, enforces risk rules, and manages two live brokerage accounts — independently, 24/5 (and 24/7 for crypto/index instruments).

---

## Project Overview

**Trade Execution Engine (TEE)** is a production-deployed, multi-layer automated trading system built with AI assistance (Claude, GPT-4) across every phase of development. It operates across four coordinated service layers running on two cloud VPS instances and a TradingView charting instance, connected by a REST + ZMQ messaging bus.

The system trades five instrument classes simultaneously: XAUUSD (Gold), USDJPY, BTCUSD, ETHUSD, and FTSE100 — managing a real-money cross-hedging strategy between a FundingPips proprietary account and a Fusion Markets personal account.

**Live since:** April 2026 (VPS #1 fully operational; VPS #2/3 provisioning in progress)
**GitHub:** https://github.com/WarrenLim1122/ArbitrageTradingStrategy (private)
**API endpoint:** https://api.warrenlimzf.com/health (live, TLS-secured)

---

## System Architecture — 4-Layer Multi-Agent Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 0 — Signal Intelligence Agent (TradingView / Pine Script v6) │
│  • M15 chart + 1D sticky-trend higher-timeframe filter              │
│  • 5-instrument portfolio: XAUUSD · USDJPY · BTCUSD · ETHUSD ·      │
│    FTSE100                                                          │
│  • Emits: {symbol, direction, entry, sl, tp, rr} via HTTPS webhook  │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ HTTPS webhook
┌──────────────────────▼──────────────────────────────────────────────┐
│  LAYER 1 — Gatekeeper Agent (FastAPI on DigitalOcean VPS #1)        │
│  • Instrument allow-list filter (6 validated symbols)               │
│  • Finnhub real-time news intelligence — suppresses signals         │
│    within ±30 min of high-impact macro events                       │
│  • Validates signal schema; drops malformed inputs                  │
│  • Passes clean signals upstream via internal REST call             │
└──────────────────────┬──────────────────────────────────────────────┘
                       │ Internal REST
┌──────────────────────▼──────────────────────────────────────────────┐
│  LAYER 2 — Orchestrator / Risk Agent (Python on VPS #1)             │
│  • Phase-aware lot sizing using static baseline equity              │
│    Phase 1: 0.67% risk/trade · Phase 2: ratio adjusts               │
│  • Dual-account routing: prop order + inverse hedge computed        │
│    independently with per-instrument pip/tick math                  │
│  • 4-kill condition monitor (30s polling interval):                 │
│    Kill 1: Daily loss ≥ 2% → FORCE_CLOSE all + halt                 │
│    Kill 2: Overall DD ≥ threshold (static floor) → halt             │
│    Kill 3: Daily profit ≥ 2.5% (Phase 2) → harvest + halt           │
│    Kill 4: Overall profit ≥ 10% (Phase 1) → permanent halt          │
│  • Telegram command bot: /phase1, /phase2, /resume, /forcestop,     │
│    /changepropfirm, /help — human-in-loop override interface        │
│  • SGT curfew scheduler: midnight force-close for all instruments   │
└──────────┬──────────────────────────────────────┬───────────────────┘
           │ ZMQ (TCP port 5555)                  │ ZMQ (TCP port 5556)
┌──────────▼──────────────────┐   ┌───────────────▼─────────────────┐
│  LAYER 3A — Prop Execution  │   │  LAYER 3B — Personal Execution  │
│  Agent (Windows VPS #2)     │   │  Agent (Windows VPS #3)         │
│  • MT5 Python API worker    │   │  • MT5 Python API worker        │
│  • Independent DD guard     │   │  • Inverse position sizing      │
│    thread (30s loop)        │   │  • Symbol map resolution        │
│  • Dynamic contract math    │   │  • Filling mode auto-detect     │
│  • Symbol map: FTSE100 →    │   │  • REP socket returns:          │
│    UK100 (broker-specific)  │   │    balance, equity, point,      │
│  • Persists DD floor to     │   │    contract_size, tick_value    │
│    config/dd_floor.json     │   └─────────────────────────────────┘
└─────────────────────────────┘
```

---

## Core Workflow and Agent Logic

### Step 1 — Signal Generation (Layer 0)
Pine Script v6 monitors 5 markets on the M15 timeframe. A 1D sticky-trend filter locks directional bias. When entry conditions are met, the script fires a structured JSON webhook to `https://api.warrenlimzf.com/webhook`. A separate backtest variant (`signal_engine_backtest.pine`) enables offline strategy validation before capital deployment.

### Step 2 — Intelligent Gatekeeping (Layer 1)
The FastAPI gatekeeper is the first line of agent-level decision-making. It:
- Validates the incoming payload schema
- Queries Finnhub's economic calendar API in real-time
- Suppresses the signal entirely if a high-impact macro event (NFP, CPI, FOMC, etc.) falls within a ±30-minute window
- Routes valid signals to Layer 2 via a local REST call

This layer prevents the system from entering positions during known volatility windows — a key intelligence filter that most retail systems skip.

### Step 3 — Risk Orchestration (Layer 2)
The orchestrator performs the most complex reasoning in the pipeline:
- Locks `baseline_equity` on `/phase1` or `/phase2` commands — lot sizes are computed from this static baseline, never from live equity, preventing compounding drift
- Applies the buffer formula: drawdown limits are tightened by 1 percentage point below the prop firm's raw limit, creating a safety buffer
- Computes per-instrument lot sizes using dynamic contract math: `lots = dollar_risk / ((sl_distance / point) × tick_value)` — correctly handles XAUUSD, index, and crypto pip structures
- Routes the directional order to the prop account and the inverse order to the personal account with independently computed sizing
- Runs a parallel kill-condition monitor thread every 30 seconds, querying live equity from both MT5 workers via ZMQ and triggering FORCE_CLOSE when any kill threshold is breached

### Step 4 — Execution (Layer 3)
Two independent MT5 execution workers (Windows VPS instances) receive JSON order payloads via ZMQ REQ/REP. Each worker:
- Resolves broker-specific symbol names via `config/symbol_map.json`
- Auto-detects order filling mode per instrument
- Returns execution telemetry (balance, equity, contract metadata) back to Layer 2
- Runs an independent static drawdown guard thread — this is a redundant kill system that acts without waiting for Layer 2, ensuring protection even if the orchestrator becomes unreachable

---

## AI-Driven Development Workflow

Claude (claude-sonnet-4-6) and GPT-4 were used as primary AI development partners throughout the entire build:

| Phase | AI Contribution |
|-------|----------------|
| Architecture design | Multi-layer agent topology, ZMQ vs REST routing decisions, VPS resource planning |
| Layer 0 | Pine Script v6 logic refinement, HTF filter design, backtest variant structuring |
| Layer 1 | FastAPI endpoint design, Finnhub integration, async news filtering |
| Layer 2 | Kill condition logic, phase-aware lot sizing, Telegram bot architecture, asyncio threading model |
| Layer 3 | MT5 Python API integration, dynamic contract math (XAUUSD pip fix), ZMQ worker patterns |
| Infrastructure | systemd service configs, nginx reverse proxy, certbot TLS, DigitalOcean firewall rules |
| Debugging | Threading race conditions (asyncio event loop isolation), XAUUSD tick value bug, ZMQ REP reply format |
| Risk validation | Buffer formula review, kill condition ordering, static vs live equity distinction |

Typical session workflow:
1. Architect a new sub-system with Claude — high-level design discussion
2. Generate initial implementation with Claude code generation
3. Iterative debugging loop with Claude reading error traces and suggesting targeted fixes
4. Claude validates edge cases (e.g., what happens if VPS #2 is unreachable during a kill condition check)
5. Deploy to VPS and validate via health checks and Telegram bot responses

Estimated AI-assisted development hours saved: **200+ hours** across the full 4-layer system build. Tasks that would take days of manual debugging (ZMQ threading, MT5 Python API quirks, async polling architecture) were resolved in hours through AI pair programming.

---

## Measurable Outcomes

| Metric | Value |
|--------|-------|
| System layers deployed | 4 (Layers 0–2 live; Layer 3 provisioning) |
| Instruments covered | 5 (XAUUSD, USDJPY, BTCUSD, ETHUSD, FTSE100) |
| Kill conditions monitored | 4 (parallel, 30s interval) |
| Monitoring frequency | Every 30 seconds, 24/7 |
| Execution latency target | Sub-second from webhook to MT5 order |
| Manual intervention required | Near-zero during normal operation (Telegram override only) |
| Backtest signal accuracy (top 3 instruments) | 78–81% directional accuracy |
| Monthly infrastructure cost | ~$18–98 USD/month (scaling with VPS count) |
| Lines of code (AI-assisted) | ~3,000+ across all layers |
| Development time with AI vs solo estimate | ~6 weeks vs 6+ months estimated alone |

---

## Why This Project Is Technically Difficult

1. **Cross-broker synchronization** — two MT5 instances at different brokers must receive orders within milliseconds of each other; ZMQ handles this with REQ/REP pattern
2. **Per-instrument contract math** — XAUUSD, indices, and crypto all have different lot-to-dollar-risk translations; the formula is generalized but instrument-specific parameters require broker-level queries
3. **Redundant kill systems** — Layer 3's independent DD guard means the prop account is protected even if the Layer 2 orchestrator crashes
4. **Async threading isolation** — Telegram's python-telegram-bot library requires its own asyncio event loop when run in a non-main thread; this required a non-obvious architecture fix
5. **Static baseline equity** — using live equity for lot sizing would cause compounding that violates prop firm rules; the system locks baseline at phase activation and never drifts
