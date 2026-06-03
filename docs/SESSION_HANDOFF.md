# Session handoff — per-cycle fee anchor, wizard re-entry / `/rearm`, final personal-$→SGD

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single Claude agent + Warren (operator). Warren runs all Telegram `/update` deploys and VPS actions himself; agent edits code on `main` and pushes.

## Status — updated 2026-06-03
- Session 17 shipped to `main` (commits `427828d`, `2a26bad`), 107 tests pass. **Not yet deployed.**
- **Per-cycle trading-fee anchor** — fixes the bogus prop `Trading Fee: $+50,000`. Worker persists `config/fee_anchor_<login>.json` (gitignored); `/equity` reports `residual − anchor`. Reset fires on `/changepropfirm` + `/phase2` for both workers. See CLAUDE.md §Current State → Session 17.
- **`/phase1` "no prompt" bug** — was a stuck conversation (no `allow_reentry`). Now all 7 wizards have `allow_reentry=True`.
- **`/rearm`** added — clears `soft_kill_override_day` to re-arm today's K1/K3 after an accidental `/resume`. In `/help`.
- **Personal-`$`→SGD** — last 3 wizard baseline echoes fixed; full audit clean.
- Sessions 15 + 16 are also still pending deploy (symbol mapper, deal-history tz fix) — same `/update` batch.

## Next actions
1. **Deploy:** `/update layer2` + `/update layer3` ×2 (both VPSes — `_worker_core.py` changed). No `uv sync`.
2. **VPS #2 only:** confirm `.env` `MT5_TERMINAL_PATH` points at the Fusion-branded terminal; confirm worker connected to the intended account (`448196` vs `459166` — Warren was switching accounts this session; verify via the worker's `MT5 connected — account=…` line or `/health`).
3. After restart: `/checksymbols`; close one trade (alert ≤30s, real P&L, no `(est.)`); then run **`/changepropfirm` or `/phase2` once** to capture the fee anchor (else prop fee still reads `$+50,000`).
4. Start Phase 1 when Warren asks: `/phase1` → `4500:1000` → `CONFIRM` (do NOT auto-start — he configures it himself).

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (work on `main`)

## Open items
- Account switch on VPS #2 (personal): Warren updating `.env`/MT5 between `448196` and `459166` this session — not confirmed which the worker finally bound to. Verify post-deploy.
- Optional offer (declined this session): make the `/phase1` prompt example adapt to the live target instead of the fixed `9000:2000`. Warren preferred to just know the ÷2 scaling rule.
- Lower-priority queued (pre-existing): folder reorganization (deletion table at git `accd561`); optional message-structure spec in TECHNICAL.md.

## Pick up here
Tell Warren the deploy batch (`/update layer2` + `/update layer3` ×2), and that prop `/equity` Trading Fee stays `$+50,000` until he runs `/changepropfirm` or `/phase2` once to set the fee anchor.
