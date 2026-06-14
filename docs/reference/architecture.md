# Architecture & Data Flow

Cross-hedging trade-execution engine. **Personal account follows the signal direction; the
prop-firm account executes the inverse as a hedge.** Sizing is phase-dependent and controlled
entirely from Telegram.

## Layer map

```
TradingView (15m chart, one chart per pair)
  └── layer0/1D-15m Breakout INDICATOR.pine   (signal engine — frozen, managed on TV)
        │  HTTPS webhook → https://api.warrenlimzf.com/signal
  layer1/main.py            VPS #1, port 8000 (public)   — Gatekeeper
        │  internal HTTP POST → LAYER2_URL (port 8001)
  layer2/logic_core.py      VPS #1, port 8001 (internal) — Logic Core
        │  ZeroMQ
        ├── PUSH tcp://<pers_ip>:5555 / REQ tcp://…:5556 → layer3/worker_personal.py  (VPS #2, Windows)
        └── PUSH tcp://<prop_ip>:5555 / REQ tcp://…:5556 → layer3/worker_prop.py      (VPS #3, Windows)
  Telegram Bot API ←→ layer2/telegram_handlers.py (runs inside the logic_core process)
```

VPS map, IPs, noVNC links, billing → `CLAUDE.md §Infrastructure`. ZMQ addresses are configured
in `config/risk_params.json → layer3_zmq` (Layer 3 **binds**, Layer 2 **connects**).

## What each layer does

### Layer 0 — Signal engine (`layer0/*.pine`)
Pine Script v6 on a 15m chart per pair. Emits a webhook JSON payload to Layer 1 on breakout.
**Frozen — do not edit without asking Warren.** The webhook contract is the 14-field JSON in
[[webhook-payload-contract]] (memory); L1 needs 9 fields, L2 needs 14. `str.tostring(na)`→"NaN"
is a known 422 trap. Pine details: `TECHNICAL.md §Layer 0`.

### Layer 1 — Gatekeeper (`layer1/main.py`, FastAPI, ~216 lines)
`receive_signal` (`layer1/main.py:126`): parse+validate payload → run the **news filter**
(`check_news_window` in `layer1/news_filter.py`, backed by `ff_calendar.py` / Finnhub) → if a
high-impact event is within ±`NEWS_WINDOW`, suppress and send **one** Telegram alert per
`(ticker, event_time)` pair; otherwise forward the raw body to Layer 2 over internal HTTP.
Returns `suppressed` / `forwarded` / 422 (bad payload) / 502-503 (L2 down).
`ALLOWED_PAIRS` / `_TICKER_CURRENCIES` derive from `layer2.symbols` (→ `config/symbols.json`).

### Layer 2 — Logic Core (`layer2/logic_core.py` ~1700 lines + `telegram_handlers.py` ~4200 + `state.py` ~490)
The brain. Three responsibilities:
1. **Signal handling** — `receive_signal` (`logic_core.py:1282`): the gate chain + geometry +
   pre-flight check + dispatch. See [calculations.md](calculations.md) and the gate list below.
2. **Monitoring** — background threads (below) poll equity, run kill conditions, detect closes,
   and run the news pre-close sweep.
3. **Telegram** — all commands + all outgoing message text live in `telegram_handlers.py`.
   `logic_core` is pure orchestration; it never builds message strings inline.
   See [messages.md](messages.md).

`state.py` holds shared config/state: phase config, propfirm config, consistency log, trading
window, locks, currency/format helpers, and the `_apply_buffers` safety-margin logic.

### Layer 3 — Execution workers (`layer3/_worker_core.py` ~1670 lines)
`worker_personal.py` and `worker_prop.py` are 20-line shims that set env + call
`_worker_core.main()`. One process per account, on its own Windows VPS, talking to a local MT5
terminal. Receives execution tickets (ZMQ PULL), answers queries (ZMQ REP), executes orders,
enforces a local static-DD guard + SGT kill switch, and runs the journaling pipeline on close.
See [execution.md](execution.md).

## Signal flow (end to end)

1. TradingView fires → Layer 1 `/signal`.
2. Layer 1 news filter → forward raw body to Layer 2 `/signal` (port 8001).
3. Layer 2 `receive_signal` runs the **gate chain** (order matters), all in `logic_core.py:1282`:
   1. SGT curfew / weekend (`_is_sgt_curfew`) → reject inline.
   2. `permanently_halted` → `msg_signal_blocked_p_halt`.
   3. not `active` (stopped / day-halted) → `msg_signal_skipped_halted`.
   4. News suppression (`_news_suppressed_pairs`, phase≠1 only) or manual `/closepair`
      (`_manual_suppressed_pairs`) → `msg_signal_suppressed`.
   5. Max open positions (`max_open_positions`, default 2, counted by prop positions) →
      `msg_signal_skipped_max_pos`.
   6. Query prop + personal contract info via ZMQ (`_query_equity` with ticker).
   7. `trade_allowed=False` on either MT5 → block (`msg_signal_blocked_algo_disabled`).
   8. `baseline_equity ≤ 0` → `msg_baseline_missing`.
   9. Compute geometry: Phase 1 → `phase1_strategy.compute_geometry`; else
      `phase2_strategy.compute_geometry`. A `{"reject": …}` → `msg_geometry_reject`.
   10. **Pre-flight** `order_check` on BOTH legs in parallel; if either rejects, place
       **nothing** (`msg_signal_not_placed_preflight`). Prevents orphan legs.
   11. PUSH both tickets (prop then personal) → spawn `_verify_and_notify` (5s fill check +
       Trade Opened alert).
4. Layer 3 worker PULLs the ticket → `_execute_order` → market order (with retry/limit fallback).
5. On TP/SL/manual close, Layer 2's equity monitor detects the vanished position and sends the
   **Position Closed** alert with real net P&L; Layer 3's own watcher fires the journaling pipeline.

## Background threads (started at `logic_core.py:995`)

| Thread | Loop | Interval | Does |
|---|---|---|---|
| `tg-bot` | `telegram_handlers._run_bot` | event-driven | Telegram polling + command handlers |
| `equity-monitor` | `_equity_monitor_loop` → `_run_equity_check` | 30 s | worker health, algo-disabled alerts, mismatch check, close detection, **kill conditions**, day-start rollover, auto-resume |
| `news-preclose` | `_news_preclose_loop` → `_run_news_preclose_check` | 60 s | close positions ahead of high-impact news (phase 2), suppression windows |

Layer 3 has its own threads: PULL (execution, main), REP (query responder), static-DD guard
(prop only), SGT scheduler, position-close watcher. See [execution.md](execution.md).

## ZMQ wiring

- **PUSH/PULL on :5555** — Layer 2 → Layer 3 execution tickets (fire-and-forget).
- **REQ/REP on :5556** — Layer 2 → Layer 3 synchronous queries (equity, positions, order_check,
  deal_pnl, order_status, account_mode, checksymbols, reset_fee_anchor). Timeout 3 s
  (`EQUITY_TIMEOUT` in `state.py`). Ports must be open between VPS #1 and VPS #2/#3.

## Config files (`config/`, loaded by `layer2/state.py`)

| File | Owns | Notes |
|---|---|---|
| `symbols.json` | canonical pair registry (TradingView names) | single source of truth; 33 pairs |
| `risk_params.json` | `prop_risk_pct` (0.01), `phase_multipliers`, ZMQ addresses, pip decimals | sizing constants |
| `propfirm_config.json` | `baseline_equity` (risk anchor), DD %s, targets, `day_start_equity`, `day_start_date_utc` | written by `/changepropfirm`, `/phase2`, `/setbaseline` |
| `phase_config.json` | `phase`, `active`, `last_signal_ts`, nested `phase1` block (stages/ratchet/profitable_days) | the `phase1` block is owned solely by `_phase1_*` in `state.py` — see the `_save_phase(owns_phase1=…)` guard |
| `consistency_log.json` | per-day profits (Phase 2 K5) | reset each Phase 2 cycle |
| `trading_window.json` | `current_window` / `next_window` (SGT HH:MM) | `/setwindow` |
| `symbol_map.json` | optional canonical→broker overrides | empty by default |
| `symbol_cache_<login>.json` | per-broker discovered mapping (gitignored) | written by Layer 3 |
| `fee_anchor_<login>.json` | per-cycle trading-fee anchor (gitignored) | written by Layer 3 |

Field-level detail: `TECHNICAL.md §Config Files`. Local copies of `phase1_config.json` /
`propfirm_config.json` are **empty** in this repo — the live values live on VPS #1.

## Hard constraints (full list in `CLAUDE.md §Hard Constraints`)

- Personal always trades **opposite** the prop firm.
- Lot sizing uses `baseline_equity × 0.67%`, **never** live equity.
- `baseline_equity` is the prop-only risk anchor for sizing + every kill (K1–K5). Personal has
  **no** kills; personal lots = `prop_lots × phase_multiplier`.
- Prop account stays **USD**; personal account currency is whatever MT5 reports (currently SGD).
- Phase switching is Telegram-only (`/phase1`, `/phase2`).
- MT5 connection must be **self-launched** by the worker (see [execution.md](execution.md)).
