# Layer 3 — Execution workers

`worker_personal.py` / `worker_prop.py` are 20-line shims (set env, call `_worker_core.main()`).
All logic is in `layer3/_worker_core.py` (~1670 lines). One process per account, each on its own
Windows VPS with a local MT5 terminal. MT5 operational gotchas: `TECHNICAL.md §MT5 Gotchas` and
`docs/MT5_VPS_Connection_Postmortem.md`.

## Threads (`main()`, `_worker_core.py:1652`)

- **PULL loop** (`_pull_loop`, main thread) — receives execution tickets on :5555, calls
  `_execute_order`. Also handles `FORCE_CLOSE` / kill messages.
- **REP loop** (`_rep_loop`) — answers Layer 2 queries on :5556 (see protocol below).
- **Static-DD guard** (`_static_dd_guard_loop`, prop only) — local backstop that force-closes if a
  static overall-DD floor is breached even when Layer 2 is unreachable.
- **SGT scheduler** (`_sgt_scheduler`) — local curfew/weekend kill switch.
- **Position-close watcher** (`_position_close_watcher`) — detects TP/SL closes and fires the
  journaling pipeline.

## MT5 connection (`_connect_mt5`, `_worker_core.py:197`) — the rule that wasted weeks

The `MetaTrader5` Python lib only gets an IPC pipe to a terminal **it self-launches** via
`mt5.initialize(path)`. Passing login/password/server to `initialize()`, or calling `mt5.login()`
to switch off the saved default, **kills the pipe** → `-10005` timeouts.

So the worker:
1. Resolves the terminal path (`_resolve_terminal_path`): `MT5_TERMINAL_PATH` env if set+exists,
   else glob `C:\Program Files\*MetaTrader*\terminal64.exe` (catches branded installs). Set the env
   only when multiple MT5 installs coexist on one VPS.
2. `mt5.initialize(path, timeout=120000)` — self-launches the terminal on its **saved-default account**.
3. **Hard account guard:** if `account_info().login != MT5_LOGIN` → log + `SystemExit(1)`. Never
   trades on the wrong account. `account_info() is None` → retry (no saved default configured).

One-time per VPS: open MT5 → File → Login to Trading Account → enter creds → **tick "Save password"**
→ Login → wait for green/ticking → close MT5. Full workflow (incl. the "Open an Account" company
step): `CLAUDE.md §VPS MT5 Setup`. Connection constraints memory: [[mt5-python-integration-constraints]].

`_account_mode` (`demo`/`real`/`contest`/`unknown`) is cached at connect from `account_info.trade_mode`
and shipped in replies so Layer 2 can adjust message format (MetaQuotes Demo lags deal history 2-3h).

## Symbol mapper (`layer3/symbol_mapper.py`)

Broker symbol translation is **isolated to Layer 3** — Layers 1/2 never see a broker suffix.
At startup `discover(available, login)` matches every canonical (from `config/symbols.json`) against
`mt5.symbols_get()` names (e.g. `EURUSD`→`EURUSD.a`/`.pro`/`m`), refuses cross-currency matches
(USDCNY never maps to USDCNH), and caches the result at `config/symbol_cache_<login>.json`.
`config/symbol_map.json` is an optional manual-override file (empty by default). Missing symbols log
`[ERROR]` and show in **`/checksymbols`** (per-broker SUPPORTED/FOUND/MISSING). Most exotic/NDF/pegged
pairs report MISSING on retail/prop MT5 — **expected**. A pair only trades if `/checksymbols` shows
FOUND on that broker. Registry notes: memory [[checksymbols-and-pair-registry]].

## Order execution (`_execute_order`, `_worker_core.py:717`)

Each ticket → `_resolve_symbol` → `_ensure_connected` → check `terminal.trade_allowed` (if off,
store an `algo_trading_disabled` ERROR result and stop). Filling mode is detected per symbol
(`_get_filling_mode`: IOC → FOK → RETURN).

- **Market order** (signal tickets carry `order_type=market`, or `LIMIT_ONLY_EXECUTION=false`):
  send `TRADE_ACTION_DEAL` at ask/bid with `sl`/`tp`/`deviation`/`magic`. On `TRADE_RETCODE_DONE`,
  store a FILLED result with fill price + discrepancies. On `MARKET_CLOSED`, retry the market order
  every `MARKET_RETRY_INTERVAL` for up to `MARKET_RETRY_WINDOW` (≈1 min) in a **background thread**
  (keeps the PULL loop responsive to kills); if still closed, fall back to a resting **LIMIT** order
  at the signal entry (`_place_limit_order`). Other rejects are fatal → REJECTED result.
- Results are stored in `_execution_results[signal_id]` and read by Layer 2's `order_status` query /
  the 5s `_verify_and_notify` fill check.

`_monitor_pending_order` watches a resting limit until fill/expiry. `_force_close_all` /
`_force_close_ticker` stamp a `close_reason_override` (mapped via `_FORCE_CLOSE_REASON_MAP`:
`daily_loss_limit→KILL_1`, `overall_drawdown_limit→KILL_2`, `daily_profit_cap→KILL_3`,
`profit_target→KILL_4`, `consistency_rule→KILL_5`, `phase1_stage_reached→STAGE_REACHED`, …) so the
journal renders the right reason.

## ZMQ REP query protocol (`_rep_loop`, `_worker_core.py:1480`)

| `query` | Builder | Returns |
|---|---|---|
| `equity` (default) | `_build_equity_reply(ticker, want_fee)` | balance/equity/profit, contract info, `account_currency`, `usd_to_acct_rate`, `trade_allowed`; + fee fields iff `want_fee` |
| `positions` | `_build_positions_reply` | open positions list |
| `order_status` | `_build_order_status_reply(signal_id)` | stored execution result |
| `order_check` | `_build_order_check_reply(msg)` | pre-flight feasibility (`verdict` ok/reject/transient, margin) |
| `deal_pnl` | `_build_deal_pnl_reply(symbol, ticket)` | realized gross/commission/swap/net for a closed position |
| `account_mode` | `_build_account_mode_reply` | demo/real/… |
| `checksymbols` | `_build_checksymbols_reply` | per-broker SUPPORTED/FOUND/MISSING |
| `reset_fee_anchor` | `_build_reset_fee_anchor_reply` | re-anchors the per-cycle fee (fee→0) |

## Trading-fee reconciliation (`_build_equity_reply` + fee anchor, `_worker_core.py:1090`)

The "Trading Fee" in `/equity` is the **all-in** broker cost (commission + swap + any fee), derived
by reconciliation, not by trusting MT5's commission field (which under-reports swap):

```
residual = balance − Σ(every deal.profit)          # Σprofit = deposits + gross realized P&L
trading_fee = residual − fee_anchor                # per-cycle
```

- `_fee_scan` sums over full deal history with `to = UTC-now + 1 day` (server-tz lead, see below).
- The **anchor** (`config/fee_anchor_<login>.json`, gitignored) is the residual captured at cycle
  start. Reporting `residual − anchor` makes the figure **per-cycle** (since the last
  `/changepropfirm` or `/phase2`) and cancels the offset from an **unbooked deposit** (a fresh demo
  with no balance-type deal would otherwise show `Trading Fee: $50,000`).
- Layer 2 fires `reset_fee_anchor` on **both** workers (prop + personal, via
  `zmq_helpers._dispatch_fee_anchor_reset`) after `/changepropfirm`, `/phase2`, **and `/phase1`** —
  each is a fresh cycle. Until a reset fires after deploy, prop `/equity` shows the bogus `$+50,000`
  — run one once. **Both workers must be on session-17+ code** for the reset to take; a worker on
  older code ignores the `reset_fee_anchor` query and keeps showing the full since-open residual
  (the `personal −SGD 12.40` vs `prop $0` split = personal worker not yet restarted with new code).
- **Gated:** the full-history scan runs only when `want_fee=True` (the `/equity` command), never on
  the 30 s monitor poll.

## Deal P&L for the close alert (`_build_deal_pnl_reply`, `_worker_core.py:1394`)

Matches the just-closed deal **strictly by `position_id` (ticket) + `DEAL_ENTRY_OUT`** — never by
symbol+latest. This prevents pairing one ticket's metadata with another same-symbol trade's P&L. If
the ticket's exit deal hasn't surfaced yet → `found=False` so the caller waits / shows `(est.)`
rather than a confident-wrong number. **`to_dt = UTC-now + 1 day`** because MT5's `deal.time` is in
the **trade server timezone (≈UTC+2/+3), not UTC** — a tight `now+30s` upper bound excludes a
just-closed deal for hours. This was the real cause of journal lag / `(est.)` alerts. Memory:
[[mt5-deal-history-server-timezone]].

## Journaling pipeline (`layer3/journal/`)

`handle_closed_position` (`journaling_worker.py:324`) runs on close:
1. **Phase 1 (immediate):** screenshot from snapshot + tick data — no deal history needed
   (`_take_screenshot_immediate` → `screenshot_capture.py`, R:R chart via `rr_chart_renderer.py`,
   upload via `storage_uploader.py`).
2. **Phase 2 (may lag):** fetch deal history (`_get_deals`, same server-tz window fix), compute
   gross/commission/swap/net, R:R, outcome, close reason (override or MT5 deal reason), then write
   Firestore (`firebase_journal.py`) carrying the Phase-1 screenshot URL.
3. If the exit deal hasn't surfaced, inline retry with backoff summing ≈735 s (chosen to outlast
   Layer 2's 600 s close-alert cap so "Journal Queued" fires *after* the close report). Still
   missing → `_enqueue_pending_deal` to a persistent queue (`pending_deals_queue.py`) for later
   retry; the queue carries the screenshot forward so it's never lost.

Firebase creds: `secrets/firebase-service-account.json`. Architecture: `TECHNICAL.md §Trade Journal`.
