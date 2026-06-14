# 09 — Deploy Runbook (CP-2 onward) + Go-Live Gates

Warren runs deploys; the agent prepares everything and tells him the exact steps. Demo-first is a hard
gate. Reference model: `docs/reference/deployment.md`.

## Hosts
| Process | Host | Restart |
|---|---|---|
| Receiver | Linux VPS (fresh droplet, or another host — confirm at CP-1) | **systemd** (auto-restart). |
| Worker | Windows VPS with the trading account's MT5 terminal | **manual** — Ctrl+C the PowerShell process + re-run. `git pull` alone does NOT reload code. |

## One-time MT5 connect (Worker VPS)
1. Open MT5 → File → Login to Trading Account → enter the account creds → **tick "Save password"** →
   Login → wait for the green/ticking connection → **close MT5**.
2. The worker self-launches that terminal via `mt5.initialize(path)` on its saved-default account. Never
   pass login/password/server to `initialize()` and never call `mt5.login()` (kills the IPC pipe → -10005).
3. Startup must log `MT5 connected — account=<login>` and the hard guard must confirm
   `account_info().login == MT5_LOGIN` else `SystemExit(1)`.

## Receiver setup (Linux)
1. `git clone`; `uv sync`. `secrets/.env` (Receiver vars `05 §5`) + nginx TLS for the public `/signal` URL.
2. systemd unit → `uv run uvicorn receiver.main:app --port 8000` (nginx TLS:443 → 127.0.0.1:8000).
   `sudo systemctl enable --now prop-receiver`.
3. Open ZMQ **5555** + **5556** between Receiver and Worker.
4. In Telegram: run **`/changepropfirm`** (sets baseline + raw limits → buffers + pushes static-DD floor),
   then **`/phase1`** (reward:risk → stages + resends the floor). Then `/status` to confirm.

## Worker setup (Windows)
1. `git clone`; `uv sync` (includes `MetaTrader5`). `secrets/.env` + `secrets/firebase-service-account.json`.
2. **Verify** `FIREBASE_JOURNAL_ENABLED=true` and `FIREBASE_JOURNAL_DRY_RUN=false` (else journaling dies silently).
3. `uv run python -m worker.main`; confirm `MT5 connected — account=…`, REP + PULL bound, static-DD guard
   loaded with the **correct** floor (not a stale one — re-run `/phase1` if it fires spuriously).

## Redeploy ("update")
- Receiver change → `git pull` + `sudo systemctl restart prop-receiver` → check `journalctl`.
- Worker change → `git pull` + **Ctrl+C the worker** + re-run. (A value "stuck wrong" = worker not restarted.)
- `pyproject.toml` changed → `uv sync` first. Build a small `/update` wizard that prints these steps.

## CP-2 acceptance (demo live)
- Both services up; account guard line shows the right login.
- One test signal → ticket → demo order fills → **Trade Opened** (phase, lots, risk, RR) → close →
  **Position Closed** within ~30s with **real net P&L, no `(est.)`** → **journal entry** (currency badge =
  account currency, R:R box entry→close bar).
- Fire `reset_fee_anchor` once after deploy so `/equity` Trading Fee starts at 0.
- **Simulate a kill:** tighten a DD % on demo and confirm the matching K fires (force-close + alert + halt).

## Go-Live gates (CP-3 — all must hold)
1. ≥ **7 demo trading days** — no orphan trades, no `(est.)` closes, correct lots every trade.
2. Each kill K1–K5 verified firing correctly (test by tightening %s on demo); Phase 1 stage ratchet +
   funded transition verified.
3. Day-roll reset + auto-resume across a session boundary; consistency log locks per day (Phase 2).
4. Journal entries present and correct.
5. `/health` green; `/checksymbols` shows traded pairs FOUND on the broker; `propfirm_day_roll` matches
   the firm dashboard's "Resets In".
Only then does Warren authorize real challenge capital.
