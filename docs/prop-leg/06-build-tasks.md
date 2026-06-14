# 06 — Build Tasks (execute T0 → T14 in strict order)

Your runbook. Top to bottom, no skipping/reordering. Each task: **Goal · Reference · Spec · Tests ·
Acceptance · Commit.** TDD: tests first. Commit + push the new repo after each task. Stop only at the
CHECKPOINT tasks. **Naming rule (`00`) applies to every artifact** — no flip/inverse/mirror/other-account.

---

## ▣ T0 — CHECKPOINT CP-0: confirm + scaffold
- **STOP, confirm with Warren:** target repo path (default `~/Coding Projects/prop-leg-system`) and that
  you read `00`–`10`.
- `git init` the new repo; build the tree from `04`; create `account_config.example.json` (`05 §4`) and
  copy to `account_config.json`; `pyproject.toml` with the deps in `04`; short `README.md`; `.gitignore`.
- **Acceptance:** tree matches `04`; `pytest` collects 0, no import errors; first commit pushed.
- **Commit:** `chore: scaffold prop-leg-system skeleton + account config schema`
- **➡ After Warren confirms the path, T1–T11 run autonomously.**

---

## ▣ T1 — Port reuse modules
- **Ref:** `layer2/strategy_common.py`, `layer2/symbols.py`, `config/symbols.json`,
  `layer1/news_filter.py`, `layer1/ff_calendar.py`, `layer3/symbol_mapper.py`, `layer3/journal/`.
- **Spec:** copy to the new locations (`04`), fix imports only, strip any second-leg reference. `symbols`
  exposes the canonical pair set (`ALLOWED_PAIRS`).
- **Tests:** `tests/test_imports.py` — import everything; `symbols.load()` non-empty.
- **Commit:** `feat: port reused modules (strategy_common, symbols, news, symbol_mapper, journal)`

## ▣ T2 — `common/phase2.py` (TDD)
- **Ref:** `layer2/phase2_strategy.py`; `02 §1`.
- **Tests FIRST (`tests/test_phase2.py`, `08 §1`):** the SHORT worked example (lots 18.52, sl 1.08554,
  tp 1.08300, dollar_risk 1000.0, RR≈3.70); a LONG case; zero-stop reject; `max_lots` reject.
- **Spec:** `compute_phase2(*, ticker, signal, entry, sl, tp, price_digits, contract_size, tick_size,
  tick_value, baseline_equity, risk_pct, max_lots=0.0) -> dict|{"reject"}` per `02 §1`. Single-account
  (no `pers_*`, no ratio).
- **Commit:** `feat: phase 2 fixed-risk box geometry (single-account)`

## ▣ T3 — `common/phase1.py` (TDD)
- **Ref:** `layer2/phase1_strategy.py`; `02 §2`.
- **Tests FIRST (`tests/test_phase1.py` + `tests/test_stages.py`, `08 §2`):** `derive_stages` →
  `[104500,107250,110000]`; `active_stage_index` ratchets only; `compute_phase1` fixed lots + moving TP
  (reward_gap drives `tp_distance`); rejects: reward_gap≤0, zero stop, TP collapses onto entry/SL;
  `validate_phase1_inputs` (first_reward<target, min_days≥2, positives); `parse_reward_risk`.
- **Spec:** port `parse_reward_risk`, `validate_phase1_inputs`, `derive_stages`, `active_stage_index`,
  `compute_phase1` per `02 §2`. Drop `pers_*`/`pers_ratio`. TP placed on the profit side of `direction`.
- **Commit:** `feat: phase 1 stage-ladder fixed-lot moving-TP geometry (single-account)`

## ▣ T4 — `common/kills.py` + buffers (TDD)
- **Ref:** `_run_equity_check` + `evaluate_kills` + `state._apply_buffers`; `02 §3,§4`.
- **Tests FIRST (`tests/test_kills.py` + `tests/test_buffers.py`, `08 §3`):** each K1–K5 boundary (exact
  floors/ceilings); K1 dynamic from day_start, K2/K3/K4 static from baseline; Phase-1 priority
  K2>K1>stage-win>K4; override suppresses K1/K3/stage but NOT K2/K4/K5; buffers (`daily−1`, `overall` raw,
  `cap=target×0.25`, `consistency−1`).
- **Spec:** `apply_buffers(raw) -> dict`; `evaluate_phase1_kills(...)`; `evaluate_phase2_kills(...)`;
  pure, return `{"kill": None|"K1".."K5"|"stage_win", "permanent": bool}`.
- **Commit:** `feat: kills K1–K5 + safety buffers (pure)`

## ▣ T5 — `receiver/state.py` (TDD)
- **Ref:** `layer2/state.py` (`_apply_buffers`, `_propfirm_day`/`propfirm_day_roll`, `_money`,
  `_fmt_price`, `_ccy_prefix`, consistency-log helpers).
- **Tests FIRST (`tests/test_dayroll.py`, `08 §4`):** SGT day-roll boundary; currency render (auto from
  account_currency, no hardcoded symbol; sign before symbol); price formatting (JPY 3dp, XAU 2, XAG 4, else 5).
- **Spec:** thread-safe config IO; `current_day(now)` via `propfirm_day_roll`; money/price helpers using
  the live `account_currency`; consistency-log read/write (per-day locked profit). `baseline_equity`
  immutable except via the wizard.
- **Commit:** `feat: receiver state — config IO, day-roll, currency, consistency log`

## ▣ T6 — `receiver/zmq_client.py`
- **Spec:** `push_ticket(ticket)`; `query(req, timeout=3s)` with retry-on-timeout (3×, 3s); helpers
  `query_equity/order_check/order_status/positions/deal_pnl/reset_fee_anchor/set_parameters`. Ticket shape
  must match `05 §2` exactly.
- **Tests:** `tests/test_zmq_client.py` round-trip vs an in-process fake REP; assert ticket shape + clean timeout.
- **Commit:** `feat: ZMQ client — PUSH ticket + REQ queries (3s timeout + retry)`

## ▣ T7 — Worker: connect + execute + queries + static-DD guard
- **Ref:** `layer3/_worker_core.py` (`_connect_mt5`~197, `_resolve_terminal_path`, `_execute_order`~717,
  `_get_filling_mode`, `_place_limit_order`, `_rep_loop`~1480 + builders, `_build_equity_reply`~1090,
  `_build_deal_pnl_reply`~1394, `_static_dd_guard_loop`).
- **Spec:** `mt5_connect.py` self-launch (`mt5.initialize(path, timeout=120000)`, no login args), hard
  guard `account_info().login == MT5_LOGIN` else `SystemExit(1)`, cache `account_mode`. `execute.py`
  market + MARKET_CLOSED retry (background thread) + LIMIT fallback, filling mode IOC→FOK→RETURN,
  force-close with reason map (daily→K1, overall→K2, daily_profit_cap→K3, profit_target→K4,
  consistency→K5, stage→STAGE_REACHED). `queries.py` all REP builders (`05 §3`) with server-tz windows
  + strict `deal_pnl` matching. `static_dd_guard.py` local overall-DD backstop loading the pushed floor.
- **Tests:** filling-mode order; ticket→request mapping; fee formula; deal-window upper bound = now+1day;
  `deal_pnl` found=False with no DEAL_ENTRY_OUT; reason mapping; static-DD floor breach. Live MT5 → CP-2.
- **Commit:** `feat: worker — MT5 connect+guard, execute, REP queries, static-DD guard`

## ▣ T8 — Worker: close watcher + journaling
- **Ref:** `_position_close_watcher`, `layer3/journal/journaling_worker.py:324`.
- **Spec:** detect close → immediate screenshot path + deferred deal-history (backoff/queue); server-tz
  chart contract; currency badge = account_currency; kill-reason mapping.
- **Tests:** record builder (reason mapping, currency badge). Pipeline integration → CP-2.
- **Commit:** `feat: worker — close watcher + journaling pipeline`

## ▣ T9 — Receiver: `/signal` + gate chain + phase-aware geometry (TDD)
- **Ref:** gate chain `docs/reference/architecture.md` §Signal flow; `05 §1,§2`.
- **Spec — gate order:** curfew/window → permanently_halted → not active (stopped/daily_halted) →
  news/manual suppress → **per-pair dedup** → `max_open_positions` → `query_equity` (trade_allowed) →
  `baseline_equity≤0` block → **geometry: phase==1 → compute_phase1 (needs live equity + active stage);
  else compute_phase2** → `order_check` pre-flight → PUSH ticket + 5s verify-and-notify.
- **Tests (`tests/test_webhook_validation.py` + `tests/test_gate_chain.py`, `08 §5,§6`):** 14-field
  accept; 422 cases; each gate's outcome; Phase 1 vs Phase 2 routing; happy path → one ticket matching `05 §2`.
- **Commit:** `feat: receiver /signal + gate chain + phase-aware geometry`

## ▣ T10 — Receiver: monitor (equity, K1–K5, day-roll, consistency)
- **Ref:** `_equity_monitor_loop`/`_run_equity_check`.
- **Spec:** 30s loop: worker health alerts; close detection → Position Closed alert (real net via
  `deal_pnl`, else `(est.)`); `evaluate_*_kills` → force-close + kill alert + set halt flags + Phase-1
  stage-win/ratchet handling; at day-roll snapshot `day_start_equity`, apply `next_window`, lock the
  day's profit into the consistency log (Phase 2), clear `daily_halted` (auto-resume), reset override.
- **Tests:** each kill path + stage-win + day-roll reset with a mocked clock + equity series.
- **Commit:** `feat: receiver monitor — health, closes, K1–K5, day-roll, consistency lock`

## ▣ T11 — Receiver: Telegram (wizards + commands + alerts) + wire entrypoints + dry-run (TDD on formats)
- **Ref:** `layer2/telegram_handlers.py` (`/changepropfirm`, `/phase1`, `/phase2` wizards; kill alerts;
  format helpers); `07-telegram-spec.md`.
- **Spec:** `messages.py` all `msg_*` builders (━×12 rule, currency from account_currency); `wizards.py`
  the three ConversationHandlers (`/changepropfirm` collects raw limits → `apply_buffers` → save baseline;
  `/phase1` reward:risk + derive stages + push static-DD floor; `/phase2`); `telegram_bot.py` the command
  set in `07`. Then wire `receiver/main.py` (FastAPI + Telegram + monitor + zmq) and `worker/main.py`
  (PULL+REP+static-DD+SGT+close-watcher). `scripts/dry_run_signal.py` posts a fake 14-field webhook to a
  local receiver + fake worker → assert correct ticket + Trade Opened alert.
- **Tests (`tests/test_messages.py`, `08 §7`):** Trade Opened / Position Closed render with the account
  currency (sign before symbol, no `$+`/`$-`); prices carry no symbol; `(est.)` path; kill alerts render.
- **Acceptance:** full suite green; dry-run produces the expected ticket + alert.
- **Commit:** `feat: telegram (wizards+commands+alerts) + entrypoints + e2e dry-run green`

## ▣ T12 — CHECKPOINT CP-1: hand back
- **STOP.** Present the tree, full green `pytest`, the dry-run trace, and a **kills-fire simulation**
  (feed an equity series that trips K1, K2, K4, a stage-win, and K5 → assert the right halt + alert).
- **Ask Warren for:** `baseline_equity`, `profit_target_pct`, `max_drawdown_overall_pct`,
  `max_drawdown_daily_pct`, `min_profit_days`, the `/phase1` reward:risk pair, `consistency_threshold_pct`,
  `risk_pct` (+ keep the mode toggle?), `propfirm_day_roll`, Telegram token, Firebase creds, MT5 login,
  Receiver host. Fold into `account_config.json`. **Do not deploy until returned.**

## ▣ T13 — Deploy to DEMO → CHECKPOINT CP-2
- Per `09-deploy-runbook.md`: Receiver (systemd+nginx TLS) + Worker (MT5 one-time connect, save password).
  Open :5555/:5556. Run `/changepropfirm` then `/phase1` (sets baseline + pushes the static-DD floor).
  Point Pine alerts at the new `/signal` (or test via `dry_run_signal.py` first).
- **CP-2:** both up; account guard matches; one signal → ticket → demo order → Trade Opened → close →
  Position Closed (real net) → journal entry. Simulate one kill on demo (tighten a % → confirm K fires).

## ▣ T14 — DEMO SOAK ≥7 trading days → CHECKPOINT CP-3
- Run ≥7 trading days. Watch: fills, all kills firing correctly, stage ratchet, day-roll resets,
  consistency-log locking, journal entries. **CP-3:** present results; Warren decides go-live.

## Done-definition
T0–T11 done = full suite green + correct dry-run + kills-fire simulation. After T11 is gated by Warren
(credentials, hosting, demo time) via the checkpoints.
