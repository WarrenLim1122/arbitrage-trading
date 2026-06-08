# VPS MT5 Setup (one-time per account — the workflow that wasted weeks)

> Full debugging journey + diagnostic checklist + what NOT to chase next time: `docs/MT5_VPS_Connection_Postmortem.md`.
> Moved out of `CLAUDE.md` (2026-06-08) to keep that file a lean router. `CLAUDE.md` points here.

**Success signal: bottom-right of MT5 turns green + shows a data rate (e.g. `22.0/0.0 Mb`) AND prices in Market Watch are ticking.** If still "n/a" or "0/0 Kb" after login, the connection is dead, not just slow — try the other workflow option below.

## Option 2 (RECOMMENDED — try this first; desktop-only)

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

## Option 1 (use only if Option 2's company isn't in the list)

Download the broker's own MT5 installer from their portal:
- **Fusion Markets:** https://fusionmarkets.com/Platforms/Metatrader-5 → MT5 for Windows
- **FundingPips:** log in to fundingpips.com → dashboard → Platforms / Downloads → MT5 for Windows

Install (will go into a folder like `C:\Program Files\Fusion Markets MetaTrader 5\` — note the exact name). Then File → Login to Trading Account → enter creds → TICK "Save password" → Login → wait for green → close.

## Diagnosing failure via the Journal tab (always check first)

| Bottom-right | Journal entries when account selected | Diagnosis |
|---|---|---|
| Green + kb/s + ticking | `authorized on … through Access Point …` | ✅ Done |
| `n/a` / `0/0 Kb` | **ZERO Network entries** | Server endpoints not configured → do Option 2 (or Option 1 if company missing) |
| `n/a` | `authorization failed` | Wrong password or wrong server name |
| `n/a` | `no connection` after `scanning network` | IP-blocked from this VPS → contact broker support |

## Deploying the worker after MT5 is green

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
