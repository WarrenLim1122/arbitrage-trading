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

## MTProto user session (the prop link) — one-time, do this FIRST
1. Warren creates `api_id` + `api_hash` at https://my.telegram.org (Apps).
2. Run `scripts/mtproto_login.py` once on the Receiver host — it phone-logs the **user account** and
   writes `secrets/personal_reader.session` (gitignored). This account must be a **member of the shared
   group** that the prop bot posts to.
3. Put both the personal **control bot** and the prop **bot** and this **user account** in the same group.
4. Confirm in logs: the reader connects and receives the prop bot's messages (a Bot API token would
   receive nothing here — must be the user session).

## Receiver setup (Linux)
1. `git clone` the new repo; `uv sync`.
2. `secrets/.env` (Receiver vars `05 §5`) + `personal_config.json` MTProto block (`api_id`/`api_hash`/
   `group_chat_id`/`prop_bot_username`). No public webhook/TLS is needed — personal has no `/signal`
   endpoint (a small health port is optional).
3. systemd unit `personal-receiver.service` → `uv run python -m receiver.main`.
   `sudo systemctl enable --now personal-receiver`.
4. Open ZMQ ports **5555 (PUSH/PULL)** and **5556 (REQ/REP)** between Receiver and Worker.

## Worker setup (Windows)
1. `git clone`; `uv sync` (includes `MetaTrader5`).
2. `secrets/.env` (Worker vars `05 §5`) + `secrets/firebase-service-account.json`.
3. **Verify** `FIREBASE_JOURNAL_ENABLED=true` and `FIREBASE_JOURNAL_DRY_RUN=false` (else journaling dies
   silently — startup WARNING).
4. Run `uv run python -m worker.main` in PowerShell; confirm `MT5 connected — account=…`, REP bound, PULL bound.

## Redeploy ("update") analog
- Receiver change → `git pull` → `sudo systemctl restart personal-receiver` → check `journalctl` (confirm
  the MTProto reader reconnects).
- Worker change → on the Windows VPS: `git pull` → **Ctrl+C the worker** → re-run `python -m worker.main`.
  A value "stuck wrong after redeploy" is almost always a worker not actually restarted.
- `pyproject.toml` changed → `uv sync` on that host first.
- Build a small `/update` Telegram wizard that prints these steps.

## CP-2 acceptance (demo live — needs the PROP demo running too)
- Both personal services up; account guard line shows the correct personal login; MTProto reader connected
  and receiving the prop bot's messages (`/health` shows reader OK).
- With the prop system on demo, let it (or `scripts/dry_run_prop_event.py`) emit a **Trade Opened** →
  personal opens the **inverse** hedge (direction opposite, lots = prop_lots×mult, sl/tp swapped) → fills →
  **Hedge Opened** alert.
- Prop **Position Closed** → personal closes the matching pair → **Position Closed** alert within ~30s with
  **real net P&L, no `(est.)`** → a **journal entry** (currency badge = personal currency).
- Prop **Kill (K1–K5)** → personal closes per `kill_action` + halts → `msg_prop_kill_action`.
- After the first deploy, fire `reset_fee_anchor` once so `/equity` Trading Fee starts from 0.

## Go-Live gates (CP-3 — all must hold)
1. ≥ **7 demo trading days** alongside the prop demo: **every** prop trade mirrored correctly (direction,
   lots, swapped SL/TP), no missed or duplicated mirrors, no orphan personal positions.
2. Prop closes reconciled (personal flat when prop is flat); prop kills honored (close + halt).
3. **Reader resilience:** kill/restart the MTProto reader mid-session → `msg_reader_disconnected` fires and
   it reconnects without missing the next prop event (or alerts loudly so Warren intervenes).
4. Journal entries present and correct; `/health` green; `/checksymbols` FOUND on the personal broker.
Only then does Warren authorize real capital, starting small.
