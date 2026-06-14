# 06 — Build Tasks (execute T0 → T14 in strict order)

This is your runbook. Do the tasks top to bottom. Each task has: **Goal · Reference · Spec · Tests ·
Acceptance · Commit.** Do not skip, reorder, or merge tasks. Mark a task done only when its Acceptance
line is literally true (tests green / output shown). Stop only at the **CHECKPOINT** tasks (CP-0, CP-1,
CP-2, CP-3).

Convention: after each task, `git add -A && git commit -m "<msg>" && git push` (set up the remote at T0).
Run tests with the project's runner (e.g. `uv run --extra dev pytest` or `pytest`).

---

## ▣ T0 — CHECKPOINT CP-0: confirm + scaffold
- **Goal:** confirm target location, initialize the new repo, create the directory skeleton.
- **Spec:**
  1. **STOP and confirm with Warren:** target repo path (suggested default
     `~/Coding Projects/personal-leg-system`) and that you've read all of `00`–`09`.
  2. `git init` the new repo; create the full tree from `04-system-architecture.md` (empty `__init__.py`,
     stub files, `.gitignore`, `pyproject.toml` with the deps listed in 04, a short `README.md`).
  3. Create `config/personal_config.example.json` (schema in `05 §4`, all placeholders) and copy it to
     `config/personal_config.json`.
- **Acceptance:** tree matches `04`; `pytest` runs (collects 0 tests, no import errors); first commit pushed.
- **Commit:** `chore: scaffold personal-leg-system skeleton + config schema`
- **➡ Resume autonomously after Warren confirms the path. Everything T1–T11 runs without stopping.**

---

## ▣ T1 — Port the reuse modules (verbatim copies + import fixes)
- **Goal:** bring over the modules that are kept unchanged, so later tasks can import them.
- **Reference:** `layer2/strategy_common.py`, `layer2/symbols.py`, `config/symbols.json`,
  `layer1/news_filter.py`, `layer1/ff_calendar.py`, `layer3/symbol_mapper.py`, `layer3/journal/`.
- **Spec:** copy each into the new-repo location per the old→new table in `04`. Fix import paths only
  (e.g. `from layer2.x` → `from common.x`). Do **not** change logic. `symbols.py` must load
  `config/symbols.json` and expose the canonical pair set (becomes `ALLOWED_PAIRS`).
- **Tests:** `tests/test_imports.py` — import every ported module; assert `symbols.load()` returns a
  non-empty pair set.
- **Acceptance:** import test green.
- **Commit:** `feat: port reused modules (strategy_common, symbols, news filter, symbol_mapper, journal)`

---

## ▣ T2 — The kernel: `common/geometry.py` (TDD, the heart)
- **Goal:** the native single-leg sizing + geometry. This is the most important task — get it exactly right.
- **Reference:** `02-calculation-parity.md` (the math + worked numbers), `common/strategy_common.py`
  (`dollar_per_unit`), `layer2/phase2_strategy.py` (the structure you mirror).
- **Tests FIRST (`tests/test_geometry.py`, from `08 §1`):** the LONG worked example
  (lots==5.00, sl==1.08300, tp==1.08554, direction=="LONG", dollar_risk==1000.0), the symmetric SHORT
  case, and the zero-`sl_distance` reject. Watch them fail.
- **Spec — implement `compute_personal_geometry(*, signal, entry, signal_sl, signal_tp, price_digits,
  contract_size, tick_size, tick_value, personal_baseline, risk_pct, max_lots=0.0) -> dict`:**
  ```
  k            = dollar_per_unit(ticker, contract_size, tick_size, tick_value)
  sl_distance  = abs(entry - signal_sl)           # personal's OWN stop (the wide side)
  tp_distance  = abs(signal_tp - entry)
  if sl_distance <= 0: return {"reject": "SL distance is zero ..."}
  risk_$        = personal_baseline * risk_pct
  dollar_per_lot= sl_distance * k
  lots          = round(risk_$ / dollar_per_lot, 2)
  if lots <= 0: return {"reject": "computed lots round to 0"}
  if max_lots > 0 and lots > max_lots: return {"reject": f"lots {lots} exceed max {max_lots}"}
  return {"direction": signal, "lots": lots,
          "sl": round(signal_sl, price_digits), "tp": round(signal_tp, price_digits),
          "dollar_risk": round(risk_$, 2), "sl_distance": sl_distance, "tp_distance": tp_distance,
          "realized_rr": (tp_distance/sl_distance if sl_distance>0 else 0.0)}
  ```
  Note the `ticker` arg for `dollar_per_unit` — thread it through (xxxUSD → k=contract_size).
- **Acceptance:** all `test_geometry.py` cases green; numbers match `02 §3` exactly.
- **Commit:** `feat: native single-leg geometry (prop logic, reversed) — parity test pinned`

---

## ▣ T3 — `receiver/state.py`: config, day-roll, currency (TDD)
- **Goal:** load/save `personal_config.json`, the SGT day-roll boundary, currency/price formatting, locks.
- **Reference:** `layer2/state.py` (`_propfirm_day`, `propfirm_day_roll`/`_propfirm_roll_min`, `_money`,
  `_fmt_price`, `_ccy_prefix`).
- **Tests FIRST (`tests/test_dayroll.py`, from `08 §3`):** an SGT time before `day_roll` belongs to the
  prior trading day; at/after belongs to today; `_money(-12.5, "SGD")` → `"SGD 12.50"` (no `$`), USD → `$`.
- **Spec:** thread-safe load/save of the config dict (a lock); `current_day(now)` using `day_roll`;
  `money(amount, currency, signed)` and `fmt_price(symbol, price)` helpers (JPY=3dp, XAU=2, XAG=4, else 5);
  `ccy_prefix` → `"$"` for USD else `"<ISO> "`. **`personal_baseline` is immutable except via the explicit
  setter** (`/setbaseline`); never auto-derive from equity.
- **Acceptance:** `test_dayroll.py` green.
- **Commit:** `feat: receiver state — config IO, SGT day-roll, currency/price helpers`

---

## ▣ T4 — `receiver/halts.py`: daily + overall DD (TDD)
- **Goal:** the only risk halts (personal had none in the reference).
- **Reference:** `docs/reference/calculations.md` §Phase 2+ (K1/K2 formulas); `01-master-plan.md §5`.
- **Tests FIRST (`tests/test_halts.py`, from `08 §2`):** daily breach fires at
  `equity ≤ day_start − day_start*daily_pct/100` (day halt); overall breach at
  `equity ≤ baseline − baseline*overall_pct/100` (permanent); `soft_kill_override_day` suppresses the
  daily halt but NOT the permanent one; no-breach returns no halt.
- **Spec — pure `evaluate_halts(equity, day_start_equity, baseline, daily_pct, overall_pct,
  override_active) -> dict`:** returns `{"halt": None | "daily" | "overall"}`. Daily = day halt
  (auto-resumes next session); overall = permanent. Override suppresses daily only.
- **Acceptance:** `test_halts.py` green.
- **Commit:** `feat: personal-equity risk halts (daily + overall DD)`

---

## ▣ T5 — `receiver/zmq_client.py`: PUSH ticket + REQ queries
- **Goal:** transport from Receiver to Worker.
- **Reference:** `05 §2` (ticket), `05 §3` (queries); reference query callers in `layer2/logic_core.py`
  (`_query_equity`, `_query_order_status`, `_query_positions`, order_check dispatch).
- **Spec:** `push_ticket(ticket: dict)` on the PUSH socket; `query(req: dict, timeout=3s) -> dict` on a
  REQ socket with the 3s timeout + retry-on-timeout (reference retries 3×, 3s apart). Build helper
  wrappers: `query_equity(ticker, want_fee)`, `query_order_check(...)`, `query_order_status(signal_id)`,
  `query_positions()`, `query_deal_pnl(symbol, ticket)`, `reset_fee_anchor()`.
- **Tests:** `tests/test_zmq_client.py` — round-trip against an in-process fake REP socket (assert the
  ticket JSON shape matches `05 §2` exactly; assert timeout returns a clean error not a hang).
- **Acceptance:** zmq client test green.
- **Commit:** `feat: ZMQ client — PUSH ticket + REQ query helpers (3s timeout + retry)`

---

## ▣ T6 — Worker: connect + execute + queries
- **Goal:** the Windows-side MT5 process logic.
- **Reference:** `layer3/_worker_core.py` — `_connect_mt5`(~197), `_resolve_terminal_path`,
  `_execute_order`(~717), `_get_filling_mode`, `_place_limit_order`, `_rep_loop`(~1480) + all
  `_build_*_reply`, `_build_equity_reply`(~1090) fee logic, `_build_deal_pnl_reply`(~1394).
- **Spec:**
  - `worker/mt5_connect.py`: resolve terminal path (env `MT5_TERMINAL_PATH` else glob
    `C:\Program Files\*MetaTrader*\terminal64.exe`); `mt5.initialize(path, timeout=120000)` —
    **no login/password/server args** (that kills the IPC pipe → -10005). **Hard guard:**
    `account_info().login != MT5_LOGIN` → log + `SystemExit(1)`; `account_info() is None` → retry.
    Cache `account_mode` from `trade_mode`.
  - `worker/execute.py`: per ticket → resolve broker symbol (symbol_mapper) → ensure connected →
    check `terminal.trade_allowed` (off → store `algo_trading_disabled` result) → detect filling mode
    (IOC→FOK→RETURN) → market `TRADE_ACTION_DEAL` with sl/tp/deviation/magic. On `MARKET_CLOSED`,
    retry every interval up to ~1min in a **background thread** (keep PULL responsive), then fall back
    to a resting LIMIT at entry. Store results in `_execution_results[signal_id]`.
  - `worker/queries.py`: implement every REP builder in `05 §3` with the **exact** server-tz window
    (`now + 1 day`) for `deal_pnl`/fee scan, and strict `position_id`+`DEAL_ENTRY_OUT` matching for `deal_pnl`.
- **Tests:** structural/unit where possible (filling-mode selection, ticket parsing, fee formula
  `(balance − Σprofit) − anchor`, deal-window upper bound = now+1day). MT5 calls behind a thin wrapper
  so they can be mocked; mark live-MT5 paths as integration (run at CP-2 on the VPS).
- **Acceptance:** worker unit tests green; live MT5 deferred to CP-2.
- **Commit:** `feat: worker — MT5 self-launch + account guard, order execute, REP queries`

---

## ▣ T7 — Worker: close watcher + journaling
- **Goal:** detect closes and journal them.
- **Reference:** `layer3/_worker_core.py` `_position_close_watcher`, `_force_close_*`,
  `_FORCE_CLOSE_REASON_MAP`; `layer3/journal/journaling_worker.py:324` (`handle_closed_position`).
- **Spec:** watcher detects a vanished position → builds the close record → fires the journaling pipeline
  (immediate screenshot path + deferred deal-history path with backoff/queue). Keep the server-tz chart
  contract (bar stamps + `pos.time` + `deal.time` are server-tz; `close_time_detected` is UTC; badge
  renders account currency). Map force-close reasons (daily→DAILY_DD, overall→OVERALL_DD) — drop the
  prop K3/K4/K5 reasons.
- **Tests:** journaling record builder unit test (reason mapping, currency badge); pipeline integration
  deferred to CP-2.
- **Commit:** `feat: worker — position-close watcher + journaling pipeline`

---

## ▣ T8 — Receiver: `/signal` endpoint + gate chain (TDD)
- **Goal:** the front door. Webhook validation + the ordered gate chain → geometry → ticket.
- **Reference:** gate chain in `docs/reference/architecture.md` §"Signal flow" (steps 3.1–3.11);
  `layer1/news_filter.py`; `05 §1` webhook, `05 §2` ticket.
- **Spec — gate order (reject/skip with the right message at each):**
  1. SGT curfew / weekend / outside trading window → reject.
  2. `permanently_halted` → blocked.
  3. not `active` (stopped or `daily_halted`) → skipped.
  4. news suppression (Finnhub high-impact within ±NEWS_WINDOW) or manual `/closepair` suppression → suppressed.
  5. **per-pair dedup** — if this pair already has an open personal position/pending signal, drop the dupe
     (multiple indicators fire the same pair; [[multi-indicator-dedup]]).
  6. `max_open_positions` (count **personal** open positions via `query_positions`) → skipped.
  7. `query_equity(ticker)` for live contract data; `trade_allowed=False` → blocked.
  8. `personal_baseline <= 0` → blocked (`baseline_missing`).
  9. `compute_personal_geometry(...)`; `{"reject":...}` → geometry-reject message.
  10. **single-leg `order_check` pre-flight**; reject → place nothing.
  11. PUSH the ticket → spawn a 5s verify-and-notify (fill check + Trade Opened alert).
- **Tests (`tests/test_webhook_validation.py` + `tests/test_gate_chain.py`, from `08 §4,§5`):** 14-field
  accept; missing field / bad signal / unknown ticker / non-positive price → 422; each gate triggers its
  outcome with mocked ZMQ + clock.
- **Acceptance:** validation + gate-chain tests green.
- **Commit:** `feat: receiver /signal endpoint + ordered gate chain (single leg)`

---

## ▣ T8.5 — Prop-halt listener (TDD) — full spec in `10-prop-halt-listener.md §6`
- **Goal:** personal closes/halts the matching position when the prop bot posts a K1–K5 kill/halt alert
  in the shared Telegram group (one-way, loose coupling; the prop system is untouched and unaware).
- **Spec:** `receiver/prop_halt_listener.py` — filter group messages to the configured prop bot →
  match kill keywords → extract pair if present → `monitor.close_positions(pair|all)` + apply the action
  policy (`10 §4`) → `msg_prop_halt_action`. Personal bot must be in the group with BotFather privacy
  mode OFF. Config block `prop_halt_listener` in `personal_config.json`.
- **Tests (`tests/test_prop_halt_listener.py`):** per `10 §6` (pair-specific close, account-wide
  close+halt, non-prop sender ignored, no-keyword ignored, disabled → no-op).
- **Commit:** `feat: prop-halt listener — close/halt on prop bot's K1–K5 group alerts (personal only)`

---

## ▣ T9 — Receiver: monitor thread (equity, halts, close detect, day roll)
- **Goal:** the 30s background loop.
- **Reference:** `layer2/logic_core.py` `_equity_monitor_loop` / `_run_equity_check`; auto-resume logic.
- **Spec:** every 30s: `query_equity` (worker health → offline/online alerts); detect vanished positions
  → send **Position Closed** alert (real net via `query_deal_pnl`, else `(est.)`); run `evaluate_halts`
  → on breach force-close + send the kill alert + set `daily_halted`/`permanently_halted`; at the SGT
  day roll snapshot `day_start_equity`, apply any scheduled `next_window`, clear `daily_halted`
  (auto-resume), reset the override.
- **Tests:** halt-trigger path and day-roll reset path with a mocked clock + fake equity series.
- **Commit:** `feat: receiver monitor — health, close detection, halts, day roll, auto-resume`

---

## ▣ T10 — Receiver: Telegram (`messages.py` + `telegram_bot.py`) (TDD on formats)
- **Goal:** all commands + all alert text. See `07-telegram-spec.md` for the full list & formats.
- **Reference:** `layer2/telegram_handlers.py` (format helpers, currency rules), `docs/reference/messages.md`.
- **Spec:** `messages.py` holds every `msg_*` pure string builder (━×12 rule, bold title, `Label: value`
  rows; currency rules from `07`). `telegram_bot.py` wires the command set in `07` (drop all
  prop/phase/consistency commands; add `/mode`, `/setrisk`, `/setdailydd`, `/setoveralldd`, `/halts`,
  `/clearhalt`). Orchestration stays out of message strings.
- **Tests (`tests/test_messages.py`, from `08 §6`):** Trade Opened / Position Closed render with personal
  SGD money (no `$`, no `$+`/`$-`), prices carry no currency symbol, the `(est.)` path renders when
  `found=False`.
- **Acceptance:** message tests green.
- **Commit:** `feat: telegram bot — commands + alert catalog (single-leg, SGD-aware)`

---

## ▣ T11 — Wire entrypoints + full dry-run
- **Goal:** both services start; a simulated signal flows end-to-end with a mocked MT5.
- **Spec:** `receiver/main.py` startup wires FastAPI + Telegram thread + monitor thread + ZMQ client.
  `worker/main.py` starts PULL + REP + SGT scheduler + close-watcher threads. `scripts/dry_run_signal.py`
  POSTs a fake 14-field webhook to a locally-running receiver pointed at a fake worker; assert a correct
  ticket is produced and a Trade Opened alert is built.
- **Tests:** the full `pytest` suite green. Capture the dry-run trace for CP-1.
- **Acceptance:** entire suite green; dry-run produces the expected ticket + alert.
- **Commit:** `feat: wire receiver+worker entrypoints; end-to-end dry-run green`

---

## ▣ T12 — CHECKPOINT CP-1: hand back to Warren
- **STOP.** Present: the file tree, full `pytest` output (all green), and the dry-run trace.
- **Ask Warren to provide / confirm:** the two mode `risk_pct` values; `daily_dd_pct` / `overall_dd_pct`;
  `personal_baseline` (SGD); Telegram bot token + chat id; Firebase service-account json; the personal
  MT5 login + VPS access; the Receiver host (reuse VPS #1 or a fresh Linux droplet).
- Fold the confirmed numbers into `config/personal_config.json` (and `*.example.json` placeholders stay).
- **Do not deploy until Warren returns these.**

---

## ▣ T13 — Deploy to DEMO (after CP-1 inputs) → CHECKPOINT CP-2
- Follow `09-deploy-runbook.md`: Receiver on Linux (systemd + nginx TLS), Worker on Windows VPS (MT5
  one-time connect + save password; self-launch verified by the account-guard log line). Open ZMQ
  :5555/:5556 between hosts. Point TradingView alerts at the new `/signal` URL (or test with
  `dry_run_signal.py` against the live demo first).
- **Acceptance / CP-2:** both services up; `account_info().login` matches; one test signal → ticket →
  demo order → Trade Opened alert → close → Position Closed alert with real net P&L (no `(est.)`) →
  journal entry written. Report to Warren.

---

## ▣ T14 — DEMO SOAK ≥7 trading days → CHECKPOINT CP-3
- Let it run on demo for at least 7 trading days (hard go-live gate). Watch: fills, halts firing
  correctly, day-roll resets, journal entries, no orphan/`(est.)` issues.
- **CP-3:** present soak results; Warren decides go-live with real capital.

---

## Done-definition
The build (T0–T11) is "done" when the **full test suite is green** and the **dry-run trace** is correct.
Everything after T11 needs Warren (credentials, hosting, demo time) and is gated by the checkpoints.
