# Master Plan — Standalone Prop-Firm-Challenge Trading System

Status: **PLAN ONLY — not built.** Build is run by a fresh agent via `03-build-prompt.md`.
Calculation detail: `02-calculation-spec.md`. Naming rule: see `00` (self-contained; never reference
another account/leg or use flip/inverse/mirror/hedge in any built artifact).

---

## 1. Goal

A standalone automated system that trades its **own** signal and is built to **pass a prop-firm
challenge** and then trade the funded account safely. It sizes off a fixed baseline risk anchor, runs a
two-phase challenge with a stage ladder, and enforces five hard kills. It is a complete, independent
deployment — its own repo, its own account, its own Telegram bot.

## 2. The signal (this system's own setup)
A **breakout-fade** strategy. On a confirmed 1D/15m breakout the system enters **against** the breakout
extreme with a **tight stop just beyond the extreme** and a **far target** at the downside/upside
projection — a high reward-to-risk profile (RR ≈ 3.7 on the reference geometry). Three indicators feed
it (breakout-fade, RSI-divergence-fade, Nadaraya-Watson-fade); all emit the same 14-field webhook.
Full Pine spec: `10-signal-engine-pine.md`.

## 3. Sizing anchor (fixed baseline — never live equity)
- `risk_$ = baseline_equity × risk_pct` (default 1.0% — confirm). `baseline_equity` is the immutable
  account anchor set at challenge start via the `/changepropfirm` wizard. **Never** auto-set from MT5.
- Lots: `lots = risk_$ / (stop_distance × k)`, `k` from `dollar_per_unit` (xxxUSD → `contract_size`,
  else `tick_value/tick_size`). Stop distance = `|entry − sl|` (the tight stop). This is the same kernel
  the reference uses; ported verbatim, single-account.
- A two-mode risk toggle (`conservative`/`aggressive`, risk_pct only) is optional — **default: keep it**
  for parity with the sibling design, but it is independent of the phase machinery. Confirm at CP-1.

## 4. The challenge — two phases (PORT from the reference, single-account)

### Phase 1 — evaluation (fixed-lot, moving-TP, stage ladder)
Reference: `layer2/phase1_strategy.py`. Per trade:
- Uses **only the stop level** from the signal; the signal's far target is **not** used in Phase 1.
- `lots = fixed_risk / (stop_distance × k)` → **lots are FIXED** (a stop-out loses exactly `fixed_risk`).
- The **take-profit is calculated** to capture the current stage's reward gap:
  `tp_distance = reward_gap / (lots × k)`, where `reward_gap = active_stage − live_equity`. The TP is
  the only thing that moves trade-to-trade; realized RR climbs (≈4.5→5.5→6.5) across a losing run.
- **Stage ladder** (`derive_stages`): cumulative equity targets from `baseline + first_reward` up to
  `baseline + profit_target` over `min_profit_days` (≥2). The active stage **ratchets up only**. Hitting
  a stage = a day-halt + a counted profitable day; hitting the final stage = K4 (funded).

### Phase 2 — funded (fixed-risk box)
Reference: `layer2/phase2_strategy.py`. `risk_$ = baseline × risk_pct`, sized over the stop distance;
SL/TP taken directly from the signal (the box). Plus the consistency rule (K5).

Phase switching is Telegram-only (`/phase1`, `/phase2`). The two phases are **separate geometries by
design — never unify them.**

## 5. Kills K1–K5 (PROP hard limits — all account-wide)
Reference: `layer2/logic_core._run_equity_check` + `phase1_strategy.evaluate_kills`; buffers in
`state._apply_buffers`. Exact formulas in `02-calculation-spec.md §3`.

| Kill | Condition | Resets? |
|---|---|---|
| **K1** daily loss (DYNAMIC, from `day_start_equity`) | `equity ≤ day_start − day_start×daily_dd%/100` | day-halt, auto-resume next session |
| **K2** overall loss (STATIC, from baseline) | `equity ≤ baseline×(1 − overall_dd%/100)` | **permanent** |
| **K3** daily profit cap (Phase 2+) | `equity ≥ day_start + baseline×daily_profit_cap%/100` | day-halt |
| **K4** profit target | `equity ≥ baseline×(1 + profit_target%/100)` | **permanent** → `/phase2` |
| **K5** consistency (Phase 2) | largest single day < `consistency_threshold%` of total profit (≥2 profitable days) | **permanent** → payout |

**Safety buffers** (`_apply_buffers`, applied to the raw firm limits the operator enters):
`daily_dd% −= 1.0`; `overall_dd%` unbuffered; `daily_profit_cap% = profit_target% × 0.25`;
`consistency_threshold% −= 1.0`. Plus a **local static-DD guard** in the Worker (force-closes if the
overall floor is breached even when the Receiver is unreachable).

## 6. Decisions locked with Warren (2026-06-14)
1. **Full prop-firm challenge logic** — phases, stage ladder, K1–K5, consistency, profit target,
   baseline anchor, buffers — all ported, single-account.
2. **Signal geometry:** tight-stop / far-target (RR ≈ 3.7), described as the system's own breakout-fade.
3. **Currency: auto-detected from the trading account** (no hardcoded symbol; render whatever MT5 reports).
4. **Independent standalone system** — no coupling to, and no mention of, any other system.
5. **Own Pine indicators** — built per `10-signal-engine-pine.md`, emitting this system's direction/levels.
6. Build run by a separate agent from `03-build-prompt.md`.

## 7. Architecture — greenfield 2-service
- **Receiver** (Linux, systemd, public TLS): `/signal` endpoint + webhook validation → gate chain
  (curfew/window → permanently_halted → not active → news/manual suppress → per-pair dedup → max-open
  → contract query → **phase-aware geometry** → pre-flight → PUSH ticket); Telegram bot (challenge
  wizards + alerts); equity monitor (poll, **K1–K5**, day-roll, auto-resume, consistency-log lock).
- **Worker** (Windows): MT5 self-launch + hard account guard; PULL execute (retry/limit fallback); REP
  queries; **local static-DD guard**; position-close watcher → journaling.
Reuse from reference: Pine detection, `dollar_per_unit`, both phase strategies, kills, buffers, day-roll,
consistency log, symbol mapper, journaling, MT5 self-launch, transport, Telegram format + wizards.

## 8. Config & Telegram surface
Single config `account_config.json` = the reference `propfirm_config.json` 12 fields + phase block +
`active`/halt flags + optional mode toggle (schema in `05 §4`). Telegram keeps the full challenge command
set: `/changepropfirm` (wizard, sets baseline + raw limits → buffers), `/phase1`, `/phase2`,
`/consistency`, `/setdayroll`, `/setwindow`, `/status`, `/equity`, `/positions`, `/health`, `/stop`,
`/resume`, `/rearm`, `/setmaxpos`, `/closepair`, `/checksymbols`, `/emergency`, `/update`, `/help`.
Full list + kill-alert catalog: `07-telegram-spec.md`.

## 9. Open numbers to confirm (at CP-1, non-blocking for the code build)
`baseline_equity`; `profit_target_pct`; `max_drawdown_overall_pct`; `max_drawdown_daily_pct`;
`min_profit_days`; `first_reward`/`fixed_risk` (the `/phase1` reward:risk pair);
`consistency_threshold_pct`; `risk_pct` (and whether to keep the two-mode toggle); the
`propfirm_day_roll` SGT time matching the firm dashboard's "Resets In"; Receiver host; account login.

## 10. Hard constraints carried forward
- **Sizing uses the fixed `baseline_equity`, never live equity.** K1 daily floor is dynamic from
  `day_start_equity`; K2/K3/K4 static from baseline.
- **Phase 1 and Phase 2 are separate geometries — never unify.**
- **MT5 self-launched** by the Worker; hard guard `account_info().login == configured login` (fatal
  exit on mismatch). Never enter test/placeholder numbers as `baseline_equity` (a bad floor makes the
  static-DD guard fire every 30s and block all trades).
- **Currency auto-detected from MT5**; never hardcode a symbol.
- Demo-first: ≥7 trading days before live capital.
