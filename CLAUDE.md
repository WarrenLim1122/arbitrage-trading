# CLAUDE.md

Operational guide for Claude Code. For everything reference-shaped — risk math, kill condition formulas, layer deep-dives, MT5 gotchas, Telegram message formats, deployment gates, go-live checklist — read **`TECHNICAL.md`**.

---

## Workflow Rules

- **Auto-push to GitHub after every code change.** Warren has given standing permission for all pushes to `main`. Never wait to be reminded — commit and push immediately after making any file edits.
- **After a push, tell Warren which Telegram `/update` commands to run** — do not repeat full deployment steps in responses. Routine deployment instructions now live inside the Telegram `/update` command:
  - Layer 1/2 changes → `/update layer2`
  - Layer 3 changes → `/update layer3` (choose 1 for Personal, 2 for Prop)
  - `uv sync --extra layer3` only if `pyproject.toml` changed (mention this explicitly if relevant).
- When Warren asks how to update or deploy:
  - If the issue is covered by `/update`, tell him which subcommand to run.
  - If not covered, debug first. After resolving, ask if it should be added to `/update`.

---

## 🔔 Pending Changes — REMIND WARREN NEXT SESSION

Surface this list the first time Warren returns:

1. **Telegram close-alert P&L breakdown** — change `_send_close_alert()` in `layer2/logic_core.py` so the P&L line shows BOTH gross and net side-by-side, plus commission:
   ```
   P&L (Net):  $-34.98
   Gross:      $-29.86
   Commission: $-5.12
   Swap:       $0.00          ← omit line if swap is 0
   ```
   Replaces current two-line layout. Apply to both Personal Signal and Prop Hedge sections. Demo fallback stays single-line `P&L: $-X.XX (est.)`. Approved in principle 2026-05-13.

---

## Project

Automated Trade Execution Engine — 4-layer cross-hedging system. Personal account (Fusion Markets) follows signal direction; prop firm account (FundingPips) executes the **inverse** as a hedge. Sizing is phase-dependent, controlled via Telegram.

## Architecture

```
TradingView (15m chart — one chart per pair)
  └── layer0/1D-15m Breakout INDICATOR.pine
        │  [HTTPS webhook]
  layer1/main.py          (VPS #1, port 8000 — public)
        │  [internal HTTP]
  layer2/logic_core.py    (VPS #1, port 8001 — internal)
        │  [ZeroMQ PUSH]
        ├── layer3/worker_personal.py  (VPS #2, Windows)
        └── layer3/worker_prop.py      (VPS #3, Windows)
Telegram Bot API ←→ layer2/logic_core.py
```

## Infrastructure

| VPS | Provider | IP | OS | Purpose |
|---|---|---|---|---|
| VPS #1 | DigitalOcean (SGP1) | 152.42.213.98 | Ubuntu 24.04 | Layer 1 + Layer 2 + nginx + TLS |
| VPS #2 | Vultr | 139.180.136.233 | Windows Server | worker-personal (Fusion Markets MT5) — project folder `C:\arbitrage` |
| VPS #3 | Vultr | 45.76.156.55 | Windows Server | worker-prop (FundingPips MT5) — project folder `C:\arbitrage` |

- **Public endpoint**: https://api.warrenlimzf.com/signal
- **Telegram bot**: HedgeHog (token in VPS #1 `.env`)
- **VPS #2 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=6288e88e-1ad6-468a-a584-914bd04590b1`
- **VPS #3 noVNC**: `https://console.vultr.com/subs/vps/novnc/?id=88dfe741-382d-47fe-a19c-199baa534bfc`
- **Billing**: DigitalOcean end-of-month. Vultr prepaid credit (Visa 7119 auto-charges).

VPS #1 layers run as systemd services (auto-restart). VPS #2/#3 workers run in PowerShell — must be manually restarted after VPS reboot. Do NOT close the PowerShell window; closing the noVNC browser tab is safe.

## Build Status

| Layer | Files | Status |
|---|---|---|
| 0 — Signal Engine | `layer0/1D-15m Breakout INDICATOR.pine` | ✅ LIVE — 8 alerts active, `in_trade` gate deployed 2026-04-27. **Frozen — do not edit without asking Warren first.** |
| 1 — Gatekeeper | `layer1/main.py`, `news_filter.py`, `ff_calendar.py` | ✅ LIVE — systemd on VPS #1 |
| 2 — Logic Core | `layer2/logic_core.py`, `telegram_handlers.py`, `state.py` | ✅ LIVE — pending `/update layer2` for Trade Opened reformat (session 12) |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ LIVE — pending `/update layer3` option 1 for immediate screenshot architecture (session 12) |

## Covered Instruments

8 pairs — any other ticker rejected at Layer 1:

```
EURUSD  GBPUSD  USDCHF  USDCAD  USDJPY  NZDUSD  XAUUSD  XAGUSD
```

`pip_type`: `"jpy"` for USDJPY, `"standard"` for all others.

---

## Hard Constraints

- Personal account always trades **opposite** direction to prop firm.
- Lot sizing uses `baseline_equity × 0.67%` — never live equity. Full formula: TECHNICAL.md §Lot Sizing.
- `baseline_equity` and `pers_baseline_equity` are **immutable** — only written by `/changepropfirm` wizard or `/phase2` wizard. Never auto-set from MT5 balance.
- Phase switching: Telegram-only (`/phase1`, `/phase2`).
- ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open between VPS #1 and VPS #2/#3.
- TradingView Premium required for webhook delivery.
- One TradingView chart per instrument — 8 charts, 8 pairs.
- Demo-first mandatory: ≥7 trading days before live capital.

---

## Where to look in TECHNICAL.md

| Working on… | Read TECHNICAL.md section |
|---|---|
| Risk math / lot sizing | §Immutable Risk Math |
| Kill conditions K1–K5 | §Kill Conditions (K1 dynamic, K2/K3/K4 static, K5 Phase 2) |
| SGT trading window / curfew | §Trading Window |
| Layer 3 / MT5 / order execution | §Layer 3 — Execution Workers, §MT5 Gotchas |
| Telegram alert formats (Trade Opened / Closed) | §Telegram Alert Formats |
| Trade journal pipeline | §Trade Journal Architecture |
| Config file fields | §Config Files |
| Deployment / `/update` internals | §Deploying Code Changes |
| Pre-live checklist | §Deployment Gates / §Go-Live Checklist |

---

## Current State (as of 2026-05-13, session 12)

All four layers deployed and operational. Gate D demo run started 2026-04-25 (7-day window passed; proceed to live when ready).

**Next action**: Run `/update layer2` on VPS #1 (Trade Opened format), then `/update layer3` → option 1 (Personal) on VPS #2 (immediate screenshot + `SCREENSHOT_ONLY_FOR_TP_SL` fix). Then switch to real Fusion Markets + FundingPips accounts when ready.
