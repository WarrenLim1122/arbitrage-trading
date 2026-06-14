# 02 — Calculation Spec (geometry + kills, exact)

All formulas are ported from the reference prop-side code and pinned by tests. Deep derivation:
`docs/reference/calculations.md`. **Single-account** — drop every second-leg output as you port.
Describe everything in this system's own terms (no flip/inverse/mirror/other-account).

## §0 Kernel (copy verbatim from `layer2/strategy_common.py`)
```python
def dollar_per_unit(ticker, contract_size, tick_size, tick_value):
    if ticker.endswith("USD") and contract_size > 0:
        return contract_size            # xxxUSD: P&L already in account-ccy/unit
    return tick_value / tick_size       # else broker tick math
```
`k = dollar_per_unit(...)`; dollars at price-distance `D` = `D × k`.

## §1 Phase 2 geometry — fixed-risk box (`phase2_strategy.compute_geometry`, single-account)
SL/TP come straight from the signal. The **stop distance is the tight side** (`|entry − sl|`).
```
risk_$        = baseline_equity × risk_pct
stop_distance = abs(entry - sl)                       # reject if <= 0
k             = dollar_per_unit(ticker, contract_size, tick_size, tick_value)
dollar_per_lot= stop_distance × k
lots          = round(risk_$ / dollar_per_lot, 2)     # reject if 0 or > max_lots
direction     = signal
out_sl        = round(sl, price_digits)
out_tp        = round(tp, price_digits)
```
**Worked (SHORT breakout-fade, EURUSD, baseline 100000, risk_pct 1%):**
entry=1.08500, sl=1.08554 (tight, above), tp=1.08300 (far, below), k=100000.
```
risk_$        = 1000.0
stop_distance = |1.08500 - 1.08554| = 0.00054
dollar_per_lot= 0.00054 × 100000 = 54.0
lots          = round(1000/54, 2) = 18.52
direction=SHORT ; sl=1.08554 ; tp=1.08300
risk taken    = 18.52 × 0.00054 × 100000 = 1000.0      # exactly baseline×pct
target gain   = 18.52 × 0.00200 × 100000 = 3704.0      # RR = 3704/1000 = 3.70
```

## §2 Phase 1 geometry — fixed-lot, moving-TP, stage ladder (`phase1_strategy`)
Uses **only the stop level** from the signal (the tight side); the signal's far target is unused in
Phase 1. Lots fixed by risk; the TP is calculated to win the active stage's reward gap.
```
reward_gap    = active_stage − live_equity            # reject if <= 0 (await ratchet)
stop_distance = abs(entry - sl)                       # the tight stop ; reject if <= 0
k             = dollar_per_unit(...)
lots          = round(fixed_risk / (stop_distance × k), 2)   # FIXED ; reject 0 / > max_lots
tp_distance   = reward_gap / (lots × k)               # the ONLY thing that moves per trade
direction     = signal
out_sl        = round(sl, price_digits)               # the stop (tight)
# TP placed tp_distance from entry on the profit side of `direction`:
#   SHORT → out_tp = round(entry - tp_distance, price_digits)
#   LONG  → out_tp = round(entry + tp_distance, price_digits)
# reject if out_tp collapses onto entry or out_sl (sub-precision stage gap)
realized_RR   = reward_gap / fixed_risk
```
Stages (`derive_stages(baseline, first_reward, profit_target_pct, min_profit_days)`):
```
target  = baseline × profit_target_pct / 100
n       = min_profit_days  (≥ 2)
step    = (target − first_reward) / (n − 1)
stages  = [ round(baseline + first_reward + step·i, 2)  for i in 0..n−1 ]
# stages[0]=baseline+first_reward ; stages[-1]=baseline+target (funded → K4)
```
`active_stage_index(stages, equity, prev_index)` = lowest stage strictly above equity; **ratchets only**;
returns `len(stages)` at the funded line. Validate: reward/risk/baseline > 0, target > 0,
min_profit_days ≥ 2, `first_reward < target`.

**Worked (XAUUSD, fixed_risk $1000, stop ≈ 1000 ticks @ $10/lot/tick → lots 1.00; baseline 100000,
profit_target 10% → target $10,000; first_reward $4,500; min_profit_days 3):**
`stages = [104500, 107250, 110000]`. At live equity 100000, active_stage 104500 →
`reward_gap=4500`, `lots=1.00` (fixed), `tp_distance = 4500/(1.00×k)`; realized RR = 4500/1000 = 4.5.
After a loss the active stage stays/ratchets so the gap grows → RR climbs.

## §3 Kills K1–K5 (`_run_equity_check` + `evaluate_kills`) — account-wide
Raw firm limits the operator enters are passed through `_apply_buffers` FIRST (see §4); the kills use the
**buffered** values.
```
K1 daily loss  (DYNAMIC):  equity ≤ day_start_equity − day_start_equity × daily_dd%/100
                           → FORCE_CLOSE + day-halt (auto-resume next session)
K2 overall     (STATIC) :  equity ≤ baseline_equity × (1 − overall_dd%/100)
                           → FORCE_CLOSE + PERMANENT halt
K3 daily cap   (P2+)    :  equity ≥ day_start_equity + baseline_equity × daily_profit_cap%/100
                           → FORCE_CLOSE + day-halt
K4 profit tgt           :  equity ≥ baseline_equity × (1 + profit_target%/100)
                           → FORCE_CLOSE + PERMANENT halt → /phase2
K5 consistency (P2)     :  (largest single profitable day) / (total profit) < consistency_threshold%/100
                           AND ≥ 2 profitable days  → FORCE_CLOSE + PERMANENT halt
```
Phase 1 kill priority (`evaluate_kills`): **K2 > K1 > stage-win > K4**. No K3/K5 in Phase 1.
Phase 2+ kills run inline in the monitor. A **day-halt** auto-clears at the next session roll; a
**permanent halt** clears only via `/phase2` (or explicit edit). `/resume` sets a same-day override that
suppresses K1/K3/stage-halt; `/rearm` clears it. **Permanent kills (K2/K4/K5) ignore the override.**

## §4 Buffers (`state._apply_buffers`) — applied to raw operator input
```
daily_dd%            = raw_daily_dd% − 1.0           # firm 3% → enforce 2%
overall_dd%          = raw_overall_dd%               # NO buffer (firm closes at the exact %)
daily_profit_cap%    = profit_target% × 0.25         # 25% of target
consistency_threshold% = raw_consistency% − 1.0      # fire 1pp early
```

## §5 Day boundary (`state._propfirm_day` / `propfirm_day_roll`)
The firm resets at a configurable SGT time (`propfirm_day_roll`, default `11:00`, set via `/setdayroll`
to match the dashboard "Resets In"). Any SGT time before the roll belongs to the trading day that opened
at the roll on the previous calendar day. At rollover: apply any scheduled `next_window`, lock the
completed day's profit into the **consistency log** (Phase 2), reset `day_start_equity`. Erring late is
safe; erring early re-opens the daily allowance before the firm does (breach risk).

## §6 Local static-DD guard (Worker backstop)
The Worker loads an overall-DD floor and force-closes if breached even when the Receiver is unreachable.
The floor is pushed by the Receiver on `/changepropfirm` and `/phase1`. If the Worker restarts with a
stale floor the guard fires every 30s and blocks all trades — fix by re-running `/phase1` (idempotent)
to resend. **Never enter placeholder numbers as `baseline_equity`** (a bad floor blocks everything).
