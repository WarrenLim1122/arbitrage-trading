# Deployment

Warren runs all deploys himself from Telegram. **The agent edits code on `main` and pushes; it does
not run VPS commands.** After a push, tell Warren which `/update` subcommand to run — don't repeat
the raw steps. The `/update` wizard itself prints them. Deploy gates / go-live checklist:
`TECHNICAL.md §Deploying Code Changes`, `§Deployment Gates`, `§Go-Live Checklist`.

## What runs where

| Process | Host | Restart |
|---|---|---|
| Layer 1 + Layer 2 | VPS #1 (DigitalOcean, Linux) | **systemd** — auto-restart; `sudo systemctl restart layer2` |
| worker-personal / worker-prop | VPS #2 / #3 (Vultr, Windows) | **manual** — Ctrl+C the PowerShell process + re-run; survive VPS reboot only if restarted |

## `/update` subcommands (text in `telegram_handlers._update_*`, ~2338–2447)

| Command | Maps to | Steps it prints |
|---|---|---|
| `/update layer2` | Layer 1 or 2 change | `git pull` → `sudo systemctl restart layer2` → check `systemctl status` / `journalctl -u layer2 -f` |
| `/update layer3` → 1 (Personal) | `worker_personal.py` change | on VPS #2: `git pull` → Ctrl+C worker → `uv run python layer3/worker_personal.py`; confirm `MT5 connected` + REP/PULL bound |
| `/update layer3` → 2 (Prop) | `worker_prop.py` change | same on VPS #3 with `worker_prop.py` |

Run `uv sync --extra layer3` **only** if `pyproject.toml` changed — mention this explicitly when it
applies. Local-toolchain note: tests run via `~/.local/bin/uv run --extra dev pytest`; `uv.lock`
stays untracked (memory [[local-test-toolchain]]).

## The worker-restart gotcha (cost real debugging time)

**Layer 3 workers do NOT pick up new code on `git pull` alone.** The Python process must be Ctrl+C'd
and re-run. Closing/reopening the noVNC browser tab does **not** restart it. A value that "stays
wrong after redeploy" is almost always a worker that was never actually restarted — confirm via the
`MT5 connected — account=…` line or that the value changed. Memory: [[mt5-python-integration-constraints]].

## Routing a change → deploy

- `layer1/*` or `layer2/*` → `/update layer2`.
- `layer3/_worker_core.py` → **both** `/update layer3` ×2 (Personal and Prop both run it).
- `layer3/worker_personal.py` only → `/update layer3` → 1.
- `layer3/worker_prop.py` only → `/update layer3` → 2.
- `layer3/journal/*` → the worker(s) that journal (both) → `/update layer3` ×2.
- `pyproject.toml` changed → add `uv sync --extra layer3` (or `--extra layer3` on the relevant host).

If a change isn't covered by `/update`, debug first; after resolving, ask Warren whether it should be
added to `/update`.

## Where live config lives

`config/propfirm_config.json` and the `phase1` block of `config/phase_config.json` are **empty in
this repo** — the real values live on VPS #1. So KB/examples should use the documented account
($50k baseline, 10% target, 3 profit days), not the local files. `symbol_cache_<login>.json` and
`fee_anchor_<login>.json` are per-VPS and gitignored.

## Post-deploy confirmations worth running

- `/health` → both legs green.
- `/checksymbols` → after a symbol-mapper change.
- Close one trade → close alert ≤30 s with real net P&L, no `(est.)` (confirms the deal-window fix).
- `/changepropfirm` or `/phase2` once → captures the per-cycle fee anchor (else prop `/equity` shows
  the bogus `$+50,000` Trading Fee).
