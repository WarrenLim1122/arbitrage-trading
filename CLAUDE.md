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
| 2 — Logic Core | `layer2/logic_core.py`, `telegram_handlers.py`, `state.py` | ✅ LIVE — Phase 1/Phase 2 strategy split shipped (Phase 1 = dynamic reward-targeting; phase-aware Trade Opened context). **Critical phase1-persistence fix shipped session 13** (see Current State). Pending `/update layer2` (also covers Trade Opened reformat, session 12) |
| 3 — Workers | `layer3/_worker_core.py`, `worker_prop.py`, `worker_personal.py` | ✅ **Live cutover UNBLOCKED (2026-05-26)** — both VPS desktops streaming live (459166 SGD + 12250900 USD). Connection rewrite shipped (`72b3921` + `75f55f5`): self-launch + hard account guard. Awaiting `git pull` + worker start on both VPSes. See Current State + VPS MT5 Setup. |

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
- **Both live MT5 accounts MUST be USD-denominated.** The system has no multi-currency support: it reads MT5 `trade_tick_value` and `deal.profit/commission/swap` (broker returns these in account deposit currency) and labels everything `$`. A SGD personal account would not break lot sizing or kills (personal lots = `prop_lots × phase_ratio`; all kills are prop-side) but would mislabel personal P&L/risk in every alert and silently mix SGD+USD when comparing the two legs. Decided 2026-05-19: open the real Fusion Markets account in **USD**, not SGD. **If Warren re-asks the SGD/USD question, answer from `docs/Account_Currency_Decision.md` — restate its one-line conclusion + workflow, do not re-derive from the code.**
- Phase switching: Telegram-only (`/phase1`, `/phase2`).
- **MT5 connection (Layer 3):** the `MetaTrader5` lib only gets IPC for a terminal **it self-launches** via `mt5.initialize(path)`. Runtime account switching (creds in `initialize()` or `login()` off the saved default) kills the pipe → `-10005`. **A terminal whose generic install has no broker server endpoints configured will silently never even attempt to connect** — Journal stays empty when you select that account, bottom-right shows `n/a` / `0/0 Kb`, prices appear "frozen" at stale values. This was the multi-week 2026-05 blocker (NOT funding/feed-side, as wrongly diagnosed earlier — both accounts streamed on mobile fine). Fix = follow one of the two workflows in **VPS MT5 Setup** below. Code enforces hard guard `account_info().login == MT5_LOGIN` (fatal exit on mismatch, never trades on the wrong account).
- ZeroMQ ports 5555 (PUSH/PULL) and 5556 (REQ/REP) must be open between VPS #1 and VPS #2/#3.
- TradingView Premium required for webhook delivery.
- One TradingView chart per instrument — 8 charts, 8 pairs.
- Demo-first mandatory: ≥7 trading days before live capital.

---

## VPS MT5 Setup (one-time per account — the workflow that wasted weeks)

**Success signal: bottom-right of MT5 turns green + shows a data rate (e.g. `22.0/0.0 Mb`) AND prices in Market Watch are ticking.** If still "n/a" or "0/0 Kb" after login, the connection is dead, not just slow — try the other workflow option below.

### Option 2 (RECOMMENDED — try this first; desktop-only)

Use the generic MetaQuotes MT5 (from metaquotes.com) + the **Open an Account** wizard to add the broker as a "company". This wires the correct server endpoints into the existing install — no new download needed.

1. Open the existing generic MT5 on the VPS
2. **File → Open an Account**
3. On the "List of companies" page that pops up — **THIS is the step that was missed for weeks** — select the broker's company name (or type its domain in "Find your company"):
   - **Fusion Markets** → choose **`Fusion Markets Pty Ltd`** (3rd entry, as of 2026-05-26)
   - **FundingPips** → choose **`FundingPips Corp (2)`** (2nd entry — the `(2)` matches server `FundingPips2-SIM`)
4. Click **Next** → choose **"Connect with an existing trade account"**
5. Enter login + password → select matching server from dropdown → **TICK "Save password"** → **Finish**
6. Wait until prices stream (bottom-right green + ticking)
7. Close MT5 — the worker will self-launch its own instance

> **Note:** Option 2 is the laptop/desktop workflow. The iPhone MT5 app handles broker selection differently — that path is unrelated and was not what got blocked.

### Option 1 (use only if Option 2's company isn't in the list)

Download the broker's own MT5 installer from their portal:
- **Fusion Markets:** https://fusionmarkets.com/Platforms/Metatrader-5 → MT5 for Windows
- **FundingPips:** log in to fundingpips.com → dashboard → Platforms / Downloads → MT5 for Windows

Install (will go into a folder like `C:\Program Files\Fusion Markets MetaTrader 5\` — note the exact name). Then File → Login to Trading Account → enter creds → TICK "Save password" → Login → wait for green → close.

### Diagnosing failure via the Journal tab (always check first)

| Bottom-right | Journal entries when account selected | Diagnosis |
|---|---|---|
| Green + kb/s + ticking | `authorized on … through Access Point …` | ✅ Done |
| `n/a` / `0/0 Kb` | **ZERO Network entries** | Server endpoints not configured → do Option 2 (or Option 1 if company missing) |
| `n/a` | `authorization failed` | Wrong password or wrong server name |
| `n/a` | `no connection` after `scanning network` | IP-blocked from this VPS → contact broker support |

### Deploying the worker after MT5 is green

1. `git pull` on the VPS for latest Layer 3 connection code
2. `.env` → `MT5_LOGIN` MUST match the MT5 saved-default account (the hard guard refuses mismatches)
3. `.env` → set `MT5_TERMINAL_PATH` ONLY when multiple MT5 installs exist on the same VPS (e.g. both generic and a broker-branded one). VPS #2 example with Fusion-branded installed:
   ```
   MT5_TERMINAL_PATH=C:\Program Files\Fusion Markets MetaTrader 5\terminal64.exe
   ```
   If only the generic MT5 is installed (e.g. typical VPS #3), leave blank — glob `C:\Program Files\*MetaTrader*\terminal64.exe` finds the single install.
4. Close all MT5 windows (worker self-launches its own)
5. `cd C:\arbitrage && uv run python layer3/worker_personal.py` (or `worker_prop.py`)
6. Expect: `MT5 connected — account=<MT5_LOGIN>  server=…  balance=…  mode=…`

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

## Current State (as of 2026-05-26)

**Live-account cutover is UNBLOCKED.** Both VPS desktops now stream live broker data and the Layer 3 connection rewrite is shipped. Awaiting deploy.

**What unblocked it (the actual root cause, after weeks of wrong diagnoses):** the generic MetaQuotes MT5 (downloaded from metaquotes.com) does NOT bake in broker server endpoints. When you select "FusionMarkets-Live" or "FundingPips2-SIM" in such an install, the terminal silently never even attempts a connection — Journal shows zero Network entries, bottom-right reads `n/a`/`0/0 Kb`, prices stay frozen at stale values. The MetaQuotes-Demo built-in account streams fine in the same install, which masked the real issue. **Both accounts were funded and streaming on mobile the whole time** (the earlier "unfunded accounts" diagnosis in `handoff/SESSION-HANDOFF.md` was wrong). Fix is purely server-endpoint config — see **VPS MT5 Setup** section above.

**Code state (`main` HEAD):**
- `72b3921` — `_worker_core._connect_mt5()` rewritten to self-launch via `mt5.initialize(path)` + hard guard `account_info().login == MT5_LOGIN`
- `75f55f5` — terminal-path glob broadened to `C:\Program Files\*MetaTrader*\terminal64.exe` so broker-branded installs (e.g. `Fusion Markets MetaTrader 5\`) are found; warns on ambiguous matches
- `.env.example` documents `MT5_TERMINAL_PATH` env (optional; only needed when multiple MT5 installs coexist)

**Verified-streaming state on the VPSes (2026-05-26):**
- VPS #2 (personal): Fusion-branded MT5 + generic MT5 both installed; `459166` is the saved default in the Fusion-branded build. **SGD-denominated** (486.88 SGD).
- VPS #3 (prop): generic MetaQuotes MT5 only, with FundingPips Corp (2) added as a company via Option 2; `12250900` is the saved default. USD-denominated ($5,000 demo).

**Next action — deploy steps (one-shot, both VPSes):**
1. On VPS #2 + VPS #3: `cd C:\arbitrage && git pull` (picks up `75f55f5`)
2. VPS #2 only: edit `.env` → add `MT5_TERMINAL_PATH=<path to Fusion-branded terminal64.exe>` (right-click the Fusion Markets MT5 desktop shortcut → Properties → copy "Target" field). Required because two MT5 installs coexist there.
3. VPS #3: no `MT5_TERMINAL_PATH` needed (only one MT5 install)
4. Close all MT5 windows on the VPS (worker self-launches)
5. `uv run python layer3/worker_personal.py` (VPS #2) and `uv run python layer3/worker_prop.py` (VPS #3)
6. Expect each log: `MT5 connected — account=<MT5_LOGIN>  server=…  balance=…  mode=…`
7. From Telegram: `/health` → both legs green

**Housekeeping when next on the VPSes:**
- Delete `C:\arbitrage\config\mt5_autologin.ini` if it still exists (leftover plaintext-password file, unused)
- `handoff/SESSION-HANDOFF.md` is stale (its "unfunded accounts" diagnosis was wrong); safe to delete or ignore — superseded by this section

**Carryover from before the MT5 saga (verify status when resuming):** `/update layer2` for the session-13 phase1-persistence fix (+ Trade Opened reformat) and the post-deploy `/phase1`→`reward:risk`→`CONFIRM`→`/resume` reconfigure; Issues 1–7 deploy via `/update layer2` + `/update layer3` opt 1+2; Telegram close-alert P&L breakdown (see Pending Changes section above).
