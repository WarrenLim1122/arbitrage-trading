# Trade Execution Engine — System Architecture

> **Note:** This is a reference document for understanding system design. It is not executable and does not configure any running service.

---

## High-Level Architecture

```
╔══════════════════════════════════════════════════════════════════════╗
║            TRADE EXECUTION ENGINE (TEE) — SYSTEM OVERVIEW           ║
║                    Production Deployment — May 2026                  ║
╚══════════════════════════════════════════════════════════════════════╝

  ┌─────────────────────────────────────────────────────────────────┐
  │  SIGNAL SOURCE — TradingView (Cloud)                            │
  │  Pine Script v6 · M15 chart · 1D HTF sticky-trend filter        │
  │  Instruments: XAUUSD · USDJPY · BTCUSD · ETHUSD · FTSE100      │
  │  Output: JSON webhook → {symbol, direction, entry, sl, tp, rr}  │
  └────────────────────────┬────────────────────────────────────────┘
                           │ HTTPS POST
                           │ api.warrenlimzf.com/webhook
                           ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │  VPS #1 — DigitalOcean SGP1 · Ubuntu 24.04                      │
  │  IP: 152.42.213.98 · Domain: api.warrenlimzf.com (TLS/nginx)    │
  │                                                                  │
  │  ┌────────────────────────────────────────────────────────────┐ │
  │  │  LAYER 1 — Gatekeeper Agent (layer1.service)               │ │
  │  │  FastAPI · Port 8000 (behind nginx 443)                    │ │
  │  │                                                            │ │
  │  │  Decision Logic:                                           │ │
  │  │  1. Validate signal schema                                 │ │
  │  │  2. Check instrument allow-list (6 symbols)                │ │
  │  │  3. Query Finnhub API → economic calendar                  │ │
  │  │  4. IF event within ±30min → DROP signal (suppress)        │ │
  │  │  5. ELSE → forward to Layer 2                              │ │
  │  └────────────────────────┬───────────────────────────────────┘ │
  │                           │ Internal REST                        │
  │  ┌────────────────────────▼───────────────────────────────────┐ │
  │  │  LAYER 2 — Orchestrator / Risk Agent (layer2.service)      │ │
  │  │  Python asyncio · Telegram Bot · ZMQ dealer                │ │
  │  │                                                            │ │
  │  │  Risk Engine:                                              │ │
  │  │  • Reads baseline_equity from propfirm_config.json         │ │
  │  │  • Computes prop lot: baseline × 0.67% ÷ contract_risk     │ │
  │  │  • Computes hedge lot: independent formula per instrument  │ │
  │  │  • Routes: LONG → prop account, SHORT → hedge account      │ │
  │  │                                                            │ │
  │  │  Kill Monitor (30s thread):                                │ │
  │  │  ┌─────────────────────────────────────────────────────┐  │ │
  │  │  │  Kill 1: Equity < day_start - 2%  → FORCE_CLOSE     │  │ │
  │  │  │  Kill 2: Equity < baseline - DD%  → FORCE_CLOSE     │  │ │
  │  │  │  Kill 3: Equity > day_start + 2.5% (P2) → CLOSE    │  │ │
  │  │  │  Kill 4: Equity > baseline + 10% (P1) → HALT        │  │ │
  │  │  └─────────────────────────────────────────────────────┘  │ │
  │  │                                                            │ │
  │  │  Command Interface (Telegram):                             │ │
  │  │  /phase1 · /phase2 · /resume · /forcestop                 │ │
  │  │  /changepropfirm · /help                                   │ │
  │  └──────────┬─────────────────────────────┬──────────────────┘ │
  └─────────────┼─────────────────────────────┼────────────────────┘
                │ ZMQ TCP :5555                │ ZMQ TCP :5556
                ▼                             ▼
  ┌──────────────────────────┐   ┌──────────────────────────────────┐
  │  VPS #2 — Windows 2022   │   │  VPS #3 — Windows 2022           │
  │  LAYER 3A — Prop Worker  │   │  LAYER 3B — Hedge Worker         │
  │                          │   │                                   │
  │  MT5 Python API          │   │  MT5 Python API                  │
  │  Broker: FundingPips     │   │  Broker: Fusion Markets          │
  │  Account: Prop firm      │   │  Account: Personal               │
  │                          │   │                                   │
  │  Features:               │   │  Features:                        │
  │  • Symbol map resolution │   │  • Inverse position sizing        │
  │  • Auto filling-mode     │   │  • Auto filling-mode              │
  │  • Independent DD guard  │   │  • Symbol map resolution          │
  │    thread (30s, static   │   │  • REP telemetry: balance,        │
  │    floor from JSON)      │   │    equity, point, tick_value      │
  │  • Persists dd_floor.json│   └──────────────────────────────────┘
  └──────────────────────────┘
```

---

## Kill Condition State Machine

```
                    ┌─────────────────────┐
                    │   SYSTEM RUNNING     │
                    │   (normal state)     │
                    └──────────┬──────────┘
                               │ Every 30 seconds
                    ┌──────────▼──────────┐
                    │  Query both MT5     │
                    │  workers via ZMQ    │
                    │  Get: equity values │
                    └──────────┬──────────┘
                               │
              ┌────────────────┴────────────────┐
              │                                 │
    ┌─────────▼──────────┐           ┌──────────▼───────────┐
    │  Kill 1 or 2?      │           │  Kill 3 or 4?         │
    │  Loss threshold    │           │  Profit threshold     │
    │  breached?         │           │  breached?            │
    └─────────┬──────────┘           └──────────┬────────────┘
              │ YES                             │ YES
    ┌─────────▼──────────┐           ┌──────────▼────────────┐
    │  FORCE_CLOSE both  │           │  FORCE_CLOSE both     │
    │  accounts (ZMQ)    │           │  accounts (ZMQ)       │
    │  Telegram alert    │           │  Telegram alert       │
    │  System HALT       │           │  Kill 4: PERMANENT    │
    └────────────────────┘           └───────────────────────┘
              │
    ┌─────────▼──────────┐
    │  Layer 3A redundant │
    │  DD guard fires     │
    │  independently      │
    │  (no L2 needed)     │
    └────────────────────┘
```

---

## Signal Flow (End-to-End)

```
TradingView signal fires
        │
        ▼ (< 100ms)
Layer 1 receives webhook
        │
        ├── Schema invalid? → 400 DROP
        ├── Symbol not in list? → DROP
        └── Finnhub event within ±30min? → DROP
        │
        ▼ (valid signal)
Layer 2 receives clean signal
        │
        ├── Query Layer 3A for {equity, tick_value, point, contract_size}
        ├── Query Layer 3B for same
        │
        ▼
Compute lot sizes
        │
        ├── prop_lot = (baseline_equity × 0.0067) / contract_risk
        └── hedge_lot = computed independently
        │
        ▼
Send ZMQ order to Layer 3A (prop: directional)
Send ZMQ order to Layer 3B (hedge: inverse)
        │
        ▼
Both workers execute via MT5 Python API
Return execution confirmation
        │
        ▼
Telegram notification sent to user
Kill monitor continues running every 30s
```

---

## Infrastructure Stack

| Component | Technology | Host |
|-----------|-----------|------|
| Signal engine | Pine Script v6 | TradingView |
| Webhook receiver | FastAPI + nginx + TLS | DigitalOcean SGP1 |
| News intelligence | Finnhub REST API | External |
| Orchestrator | Python 3.12 asyncio | DigitalOcean SGP1 |
| Command interface | python-telegram-bot | DigitalOcean SGP1 |
| Inter-service messaging | ZeroMQ (REQ/REP) | TCP between VPS |
| Prop execution | MT5 Python API | Windows VPS #2 |
| Hedge execution | MT5 Python API | Windows VPS #3 |
| Process management | systemd | Ubuntu 24.04 |
| DNS + TLS | Namecheap + Certbot | Namecheap DNS |
| Version control / CI | GitHub | GitHub |
| AI development partner | Claude (Anthropic) | API / Claude Code |
