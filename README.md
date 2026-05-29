# arbitrage-trading

Automated trade execution engine — a 4-layer cross-hedging system that runs a personal account (Fusion Markets) following signal direction while a prop firm account (FundingPips) takes the inverse as a hedge. Sizing is phase-dependent and controlled via Telegram.

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

## Layout

| Path | Purpose |
|---|---|
| `layer0/` | Pine v6 signal source (1D HTF trend + 15m swing breakout) for TradingView |
| `layer1/` | FastAPI gatekeeper — validates ticker, blocks high-impact news, forwards to Layer 2 |
| `layer2/` | Logic core — risk math, kill conditions, Telegram bot, dispatches to workers |
| `layer3/` | MT5 execution workers (Windows VPSes) + trade-journal pipeline writing to Firestore |
| `config/` | JSON runtime config — phase, risk params, trading window, prop firm limits |
| `scripts/vps-setup/` | One-time PowerShell bootstrap for VPS #2/#3 (`setup_worker_*.ps1`) |
| `scripts/dev-tests/` | Local dry-run utilities (`test_firebase_write.py`, `test_journal_dryrun.py`) |
| `secrets/` | Gitignored — Firebase service account, MT5 creds (per-VPS) |
| `docs/` | Reference docs (architecture, project overview, sample logs) |

## Where to read what

- **`CLAUDE.md`** — operational guide: workflow rules, deployment via Telegram `/update`, pending tasks, infrastructure map, hard constraints.
- **`TECHNICAL.md`** — full reference: risk math, kill condition formulas (K1 dynamic / K2-K4 static / K5 consistency), layer-by-layer deep-dives, MT5 operational gotchas, Telegram alert formats, trade journal architecture, deployment gates, go-live checklist.
- **`docs/`** — supplementary architecture and sample-log documents.

## Deployment

VPS #1 layers run as systemd services (auto-restart). VPS #2 / #3 workers run in PowerShell — must be manually restarted after VPS reboot. All deployments are driven from Telegram via `/update` (subcommands `layer2`, `layer3 1`, `layer3 2`, `account`). See `CLAUDE.md §Workflow Rules`.

## Status

All four layers deployed and operational. Gate D demo run started 2026-04-25. Trade journal pipeline (VPS #2 only) writes to Firestore collection `users/{userId}/trades`, consumed by the public journal at `warrenlimzf.com/journal`.

## Related repos

- [`personal-website`](https://github.com/WarrenLim1122/personal-website) — portfolio site that currently embeds the trade-journal UI.
- [`trading-journal`](https://github.com/WarrenLim1122/trading-journal) — standalone trade-journal frontend (Firestore reader). Embedded in `personal-website` via git submodule for now; will move to its own domain later.
