# 09 — Deploy Runbook (CP-2 onward) + Go-Live Gates

Warren runs deploys; the agent prepares everything and tells him the exact steps. Demo-first is a
hard gate. Reference deploy model: `docs/reference/deployment.md`.

## Hosts
| Process | Host | Restart model |
|---|---|---|
| Receiver | Linux VPS (reuse VPS #1 DigitalOcean, or a fresh droplet — confirm at CP-1) | **systemd** (auto-restart). |
| Worker | Windows VPS #2 (Vultr, already runs the personal Fusion MT5 terminal) | **manual** — Ctrl+C the PowerShell process + re-run. `git pull` alone does NOT reload code. |

> The Worker on VPS #2 already has the personal MT5 terminal logged in. Confirm it's the right account
> before pointing the new worker at it.

## One-time MT5 connect (Worker VPS) — the rule that wasted weeks in the reference
1. Open MT5 → File → Login to Trading Account → enter the **personal** creds → **tick "Save password"**
   → Login → wait for the green/ticking connection → **close MT5**.
2. The worker self-launches that terminal via `mt5.initialize(path)` on its saved-default account.
   **Never** pass login/password/server to `initialize()` and never call `mt5.login()` — that kills the
   IPC pipe (`-10005`).
3. Startup must log `MT5 connected — account=<login>` and the **hard guard** must confirm
   `account_info().login == MT5_LOGIN`, else `SystemExit(1)`. This line is your proof the worker is live
   on the right account.

## Receiver setup (Linux)
1. `git clone` the new repo; `uv sync` (or `pip install -e .`).
2. Put `secrets/.env` (Receiver vars from `05 §5`) + nginx TLS for the public `/signal` URL.
3. systemd unit `personal-receiver.service` → `uv run uvicorn receiver.main:app --port 8000`
   (nginx proxies TLS:443 → 127.0.0.1:8000). `sudo systemctl enable --now personal-receiver`.
4. Open ZMQ ports **5555 (PUSH/PULL)** and **5556 (REQ/REP)** between Receiver and Worker.

## Worker setup (Windows)
1. `git clone`; `uv sync` (includes `MetaTrader5`).
2. `secrets/.env` (Worker vars `05 §5`) + `secrets/firebase-service-account.json`.
3. **Verify** `FIREBASE_JOURNAL_ENABLED=true` and `FIREBASE_JOURNAL_DRY_RUN=false` (else journaling dies
   silently — startup WARNING).
4. Run `uv run python -m worker.main` in PowerShell; confirm `MT5 connected — account=…`, REP bound, PULL bound.

## Redeploy ("update") analog
- Receiver change → `git pull` on VPS #1 → `sudo systemctl restart personal-receiver` → check `journalctl`.
- Worker change → on VPS #2: `git pull` → **Ctrl+C the worker** → re-run `python -m worker.main`. A value
  that "stays wrong after redeploy" is almost always a worker that wasn't actually restarted.
- `pyproject.toml` changed → `uv sync` on that host first.
- Build a small `/update` Telegram wizard that prints these steps (port the reference `_update_*`).

## CP-2 acceptance (demo live)
- Both services up; account guard line shows the correct login.
- Send a test signal (TradingView demo alert, or `scripts/dry_run_signal.py` against the live receiver):
  ticket → demo market order fills → **Trade Opened** alert (correct lots, SGD risk, mode).
- Close it → **Position Closed** alert within ~30s with **real net P&L, no `(est.)`** (proves the
  server-tz deal window) → a **journal entry** written to Firestore with the R:R chart + SGD badge.
- After the first deploy, fire `reset_fee_anchor` once so `/equity` Trading Fee starts from 0.

## Go-Live gates (CP-3 — all must hold)
1. ≥ **7 demo trading days** with no orphan trades, no `(est.)` close alerts, correct lots every trade.
2. Daily + overall DD halts observed firing correctly at least once (test by tightening the % on demo).
3. Day-roll reset + auto-resume verified across a session boundary.
4. Journal entries present and correct (currency badge = SGD, R:R box spans entry→close bar).
5. `/health` green; `/checksymbols` shows the traded pairs FOUND on the personal broker.
Only then does Warren authorize real capital, starting small.
