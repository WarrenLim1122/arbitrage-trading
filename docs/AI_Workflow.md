# AI-Assisted Development Workflow

> **Note:** This document records how Claude (AI) was used during development of the Trade Execution Engine. It is informational only.

---

## Tools Used

**Primary AI:** Claude (claude-sonnet-4-6) via Claude Code CLI and Claude.ai  
**Secondary AI:** GPT-4 (early prototyping and cross-validation)

---

## Development Methodology: AI Pair Programming

The entire TEE system was built using a tight human-AI collaboration loop:

```
Human: Define requirement or describe bug
        │
        ▼
Claude: Propose architecture / generate implementation
        │
        ▼
Human: Review, test on VPS, identify issues
        │
        ▼
Claude: Diagnose root cause, suggest fix
        │
        ▼
Deploy → Validate → Next feature
```

This cycle repeated ~200+ times across the full system build.

---

## AI Contributions by Layer

### Layer 0 — Pine Script Signal Engine
- Generated initial Pine Script v6 signal logic based on multi-timeframe requirements
- Iterative refinement of HTF sticky-trend filter (1D bar persistence across M15 repaints)
- Designed the separate backtest variant to avoid future-leak contamination
- Structured webhook payload format for clean downstream parsing

### Layer 1 — FastAPI Gatekeeper
- Designed the FastAPI endpoint structure with async request handling
- Integrated Finnhub economic calendar API (auth headers, endpoint selection, event filtering)
- Wrote the ±30 minute time-window suppression logic with timezone-aware datetime comparison
- Debugged HTTPS reverse proxy configuration (nginx → FastAPI, TLS redirect)

### Layer 2 — Risk Orchestrator
- Designed the full asyncio architecture with separate threads for:
  - Telegram polling loop (isolated event loop to avoid RuntimeError in non-main thread)
  - Kill condition monitor (30s interval, daemon thread)
  - SGT curfew scheduler
- Designed the static baseline equity locking mechanism — prevents compounding drift that violates prop firm rules
- Wrote all 4 kill conditions and reviewed ordering logic (Kill 2 enforced on both Layer 2 and Layer 3A independently)
- Implemented the buffer formula (1pp tightening on loss limits, 0.25× on daily profit cap)
- Designed the ZMQ REQ/REP query pattern for equity polling
- Fixed `_query_equity()` dict format to return all required fields from Layer 3

### Layer 3 — MT5 Execution Workers
- Identified and fixed the XAUUSD tick value bug (`trade_tick_value` only, no ×10 multiplier)
- Designed the `_resolve_symbol()` canonical-to-broker mapping system
- Implemented `_get_filling_mode()` auto-detection logic
- Designed the independent `_static_dd_guard_loop` thread (redundant safety, acts without Layer 2)
- Structured `config/dd_floor.json` persistence for restart survivability

### Infrastructure
- Wrote all systemd unit files (`layer1.service`, `layer2.service`)
- Configured nginx reverse proxy with HTTPS redirect and proxy_pass
- Guided certbot TLS certificate issuance and auto-renewal setup
- Designed DigitalOcean firewall rules (ZMQ ports 5555–5556 restricted to VPS #1 IP only)
- Wrote `.env` file structure and environment variable loading

---

## Claude Code CLI — Key Workflows

1. **Multi-file codebase navigation** — read across all 4 layers simultaneously to understand cross-layer dependencies
2. **CLAUDE.md as single source of truth** — all architectural decisions, kill conditions, and deployment gates documented in CLAUDE.md; used as context for every session
3. **VPS SSH + Claude Code loop** — write in Claude Code → push to GitHub → pull on VPS → test via Telegram
4. **Error trace analysis** — full Python stack traces → root cause + fix in one turn
5. **Risk logic validation** — kill condition math reviewed against prop firm rules to confirm safety margins

---

## Efficiency Impact

| Task | Manual Estimate | With Claude | Saving |
|------|----------------|-------------|--------|
| ZMQ async architecture design | 3–5 days | 2 hours | ~95% |
| MT5 Python API integration | 1–2 weeks | 3 days | ~75% |
| XAUUSD tick value debug | Unknown (obscure bug) | 1 session | Unblocked |
| systemd + nginx + TLS setup | 1–2 days | 2 hours | ~80% |
| Kill condition logic design | 2–3 days | 1 day | ~65% |
| Telegram bot async threading fix | 1–3 days | 1 session | ~90% |
| **Total project** | **~6+ months** | **~6 weeks** | **~75%** |
