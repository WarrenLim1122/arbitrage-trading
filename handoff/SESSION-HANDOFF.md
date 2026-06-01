# Session handoff — universal symbol mapper (33 pairs) + TradingView webhook 422 fix

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).

**Role:** Single Claude agent + Warren (operator). Warren deploys via Telegram `/update`; Claude edits + pushes to `main`.

## Status — updated 2026-06-01
- **Shipped + pushed to `main`, NOT yet deployed:** `575af7d` (symbol mapper, 33 symbols) and `8c77009` (webhook pine + folder cleanup). Full detail in CLAUDE.md §Current State → Session 15.
- Symbol-mapper system is code-complete: `config/symbols.json` (SoT, 33 symbols), `layer2/symbols.py` (loader), `layer3/symbol_mapper.py` (broker discovery), `/checksymbols` command. 104 tests pass.
- Webhook 422 root-caused and fixed: NW indicator emitted 6 fields, L1 needs 9 / L2 needs 14. Enriched pine written to `layer0/Nadaraya-Watson Webhook INDICATOR.pine` (Option B — schemas untouched).
- Symbol-mapper discovery/validation has **only been syntax/unit-tested locally** — it has never run against a live MT5 terminal. The real per-broker FOUND/MISSING tables only exist after a worker restart on the VPS.

## Next actions
1. **Deploy:** `/update layer2` + `/update layer3` ×2 (Layer 1/2 and `_worker_core.py` both changed). No `pyproject.toml` change → no `uv sync`. Workers need a true Ctrl+C restart, not just `git pull`.
2. **After workers restart, run `/checksymbols`** — confirm which of the 33 canonicals resolve on Fusion (VPS #2) vs FundingPips (VPS #3). Expect most exotic/NDF/pegged to be MISSING.
3. **Warren, on TradingView:** paste `layer0/Nadaraya-Watson Webhook INDICATOR.pine` into the NW indicator, save, and **recreate the alert** (Any alert() function call → webhook URL) so it picks up the 14-field payload.
4. **Warren, manual:** `rmdir "Suggest To Delete"` — the folder's files are deleted/committed but the empty dir shell couldn't be removed (env EPERM on root dir deletion).

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`, clean, pushed to HEAD `8c77009`)

## Open items
- Only arm a TradingView alert for a pair `/checksymbols` shows FOUND on the broker that trades it — otherwise the signal 422s/502s or dies at Layer 3. (Two-gate model: registry opens the pair, the alert is the real switch.)
- TECHNICAL.md has no dedicated §Symbol Mapper section yet (CLAUDE.md §Covered Instruments is the pointer) — optional, not blocking.
- Pre-existing unstaged edits remain in `docs/Project_Overview.md` and `docs/System_Architecture.md` (not from this session — left untouched).

## Pick up here
Deploy the symbol-mapper (`/update layer2` + `/update layer3` ×2), then run `/checksymbols` to capture each broker's real symbol-resolution tables — that's the one piece that could only be validated on the VPS, not locally.
