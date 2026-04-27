# CLAUDE.md

Guidance for Claude Code. For full technical details — layer deep-dives, risk math, Telegram commands, kill conditions, deployment gates, go-live checklist — read **`TECHNICAL.md`**.

## Workflow Rules

- **Auto-push to GitHub after every code change.** Warren has given standing permission for all pushes to `main`. Never wait to be reminded — commit and push immediately after making any file edits.
- **Always include full deployment steps** in responses after a push: exact SSH command, `git pull`, and restart commands for every affected VPS, ready to copy-paste.

### Deployment steps after every push

**VPS #1 — Layer 1 or 2 changes:**
```bash
ssh root@152.42.213.98
cd /root/arbitrage-trading && git pull && sudo systemctl restart layer2
systemctl status layer2
```
Restart `layer1` instead if only Layer 1 changed; restart both if both changed.

**VPS #2 / VPS #3 — Layer 3 changes (noVNC PowerShell):**

Warren's workflow — always write steps this way:
1. Close the PowerShell window with the **X button** (kills the worker — Warren cannot type Ctrl+C in noVNC)
2. Open a new PowerShell window
3. Run one at a time:
```
cd C:\arbitrage
git pull
uv run python layer3/worker_prop.py
```
Use `worker_personal.py` for VPS #3. `uv sync --extra layer3` only if `pyproject.toml` changed.

---

## Project

Automated Trade Execution Engine — 4-layer cross-hedging system. Personal account (Fusion Markets) follows signal direction; prop firm account (FundingPips) executes the **inverse** as a hedge. Sizing is phase-dependent, controlled via Telegram.

## Architecture

```
TradingView (15m chart — one chart per pair)
  └── layer0/signal_engine.pine
        │  [HTTPS webhook]
  layer1/main.py          (VPS #1, port 8000 — public)
        │  [internal HTTP]
  layer2/logic_core.py    (VPS #1, port 8001 — internal)
        │  [ZeroMQ PUSH]
        ├── layer3/worker_prop.py      (VPS #2, Windows)
        └── layer3/worker_personal.py  (VPS #3, Windows)
Telegram Bot API ←→ layer2/logic_core.py
```

## Infrastructure

| VPS | Provider | IP | OS | Purpose |
|---|---|---|---|---|
| VPS #1 | DigitalOcean (SGP1) | 152.42.213.98 | Ubuntu 24.04 | Layer 1 + Layer 2 + nginx + TLS |
| VPS #2 | Vultr | 45.76.156.55 | Windows Server | worker-prop (FundingPips MT5) |
| VPS #3 | Vultr | 139.180.136.233 | Windows Server | worker-personal (Fusion Markets MT5) |

- **Public endpoint**: https://api.warrenlimzf.com/signal
- **Telegram bot**: HedgeHog (token in VPS #1 `.env`)
- **VPS #2 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=88dfe741-382d-47fe-a19c-199baa534bfc`
- **VPS #3 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=6288e88e-1ad6-468a-a584-914bd04590b1`
- **Billing**: DigitalOcean end-of-month. Vultr prepaid credit (Visa 7119 auto-charges).

## Build Status

| Layer | Files | Status |
|---|---|---|
| 0 — Signal Engine | `layer0/signal_engine.pine` | ✅ LIVE — 8 alerts active, `in_trade` gate deployed 2026-04-27 |
| 1 — Gatekeeper | `layer1/main.py`, `layer1/news_filter.py` | ✅ LIVE — systemd on VPS #1 |
| 2 — Logic Core | `layer2/logic_core.py` | ✅ LIVE — `trade_allowed` monitoring + 5s verification deployed 2026-04-27 |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ LIVE — PowerShell on VPS #2 + #3 |

**Current phase**: Gate D — 7-day demo run started 2026-04-25. Target go-live: ~2026-05-03.

VPS #1 layers run as systemd services (auto-restart). VPS #2/#3 workers run in PowerShell — must be manually restarted after VPS reboot. Do NOT close the PowerShell window; closing the noVNC browser tab is safe.

## Covered Instruments

8 pairs — any other ticker rejected at Layer 1:
```
EURUSD  GBPUSD  USDCHF  USDCAD  USDJPY  NZDUSD  XAUUSD  XAGUSD
```
`pip_type`: `"jpy"` for USDJPY, `"standard"` for all others.

---

## Known MT5 Gotchas (operational — read before touching Layer 3)

- **"Disable algorithmic trading when the account has been changed"** (MT5 → Tools → Options → Expert Advisors) must be **unchecked** on both VPS #2 and VPS #3. If checked, MT5 silently disables algo trading after any account change — orders are rejected with no error in Layer 3. Root cause of the 2026-04-24 NZDUSD silent failure. Uncheck once; it persists.
- **`trade_allowed` monitoring**: equity monitor reads this flag from both workers every 30s via ZMQ REP. Immediate Telegram alert if MT5 auto-disables algo trading, with step-by-step fix instructions.
- **5-second position verification**: after every signal dispatch, Layer 2 waits 5s, queries actual positions from both workers, and sends "Trade Confirmed ✅✅" or "⚠️ EXECUTION FAILURE ❌" with the exact error. No more silent failures.
- **XAGUSD lot sizing**: use `trade_tick_size` (0.0001), NOT `point` (0.001). Using `point` inflates lots 10×. Fixed 2026-04-22.
- **MetaTrader5 import on Linux = instant crash.** Layers 1 and 2 must never import it.

---

## Hard Constraints

- Personal account always trades **opposite** direction to prop firm.
- Lot sizing uses `baseline_equity × 0.67%` — never live equity.
- Personal lots = `prop_lots × phase_ratio`. Never compute from a separate dollar risk formula.
- Prop firm config: wizard-only (`/changepropfirm`). Never edit `propfirm_config.json` manually.
- Phase switching: Telegram-only (`/phase1`, `/phase2`).
- ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open between VPS #1 and VPS #2/#3.
- TradingView Premium required for webhook delivery.
- One TradingView chart per instrument — 8 charts, 8 pairs (NAS100 removed).
- Demo-first mandatory: ≥7 trading days before live capital.

---

## Current State (as of 2026-04-27)

All four layers deployed and operational. Gate D demo run in progress.

- Layer 0: 8 alerts active. `in_trade` gate live on all charts — no double entries.
- Layer 1: Live, news filter active, SGT curfew rejections working.
- Layer 2: `trade_allowed` monitoring + 5-second position verification deployed.
- Layer 3: Both workers running (VPS #2 prop account 5049711515, VPS #3 personal account 106260846, both MetaQuotes demo).

**Next action**: Wait for signals during trading hours (12:00–00:00 SGT, weekdays). On each signal, check Telegram for "Trade Confirmed ✅✅". Tick off Gate D checklist items as they occur. Go live ~2026-05-03.
