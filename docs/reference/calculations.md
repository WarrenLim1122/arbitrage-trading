# Calculations — risk, lot sizing, geometry, kills

The calculation engine is **pure** (no I/O) and lives in three small modules, called from
`logic_core.receive_signal` and `logic_core._run_equity_check`:

- `layer2/strategy_common.py` — shared helpers (`invert_signal`, `dollar_per_unit`).
- `layer2/phase1_strategy.py` — Phase 1 geometry, stages, kills.
- `layer2/phase2_strategy.py` — Phase 2 geometry (verbatim extraction of the original L2 math).

Risk constants live in `config/risk_params.json`; the risk anchor + DD/target settings live in
`config/propfirm_config.json`. Derivations & worked numbers: `TECHNICAL.md §Immutable Risk Math`.

## Foundational facts

- **`baseline_equity`** is the single risk anchor. It drives **prop lot sizing** and **every kill
  level (K1–K5)**. It is immutable except via `/changepropfirm`, `/phase2`, `/setbaseline`.
  Never auto-set from MT5 balance. It is entered as a **mandatory step in `/changepropfirm`**, so
  it is always configured before any trade — the `/phase1` live-balance fallback
  (`_lock_baseline_from_live`, fires only when `baseline_equity ≤ 0`) is a dead safety net that
  never triggers in practice. See memory [[baseline-always-configured]].
- **`prop_initial_deposit` / `pers_initial_deposit`** are the actual capital — used **only** for
  equity-% reporting and the fee reconciliation in `/equity`. **Zero** effect on sizing or kills.
- **The personal account has no kills and no risk baseline.** Its lots are purely
  `prop_lots × phase_multiplier`.
- **Constants** (`config/risk_params.json`): `prop_risk_pct = 0.01` (1.0% — raised from 0.67% 2026-06-14, Phase 2 only);
  `phase_multipliers = {1: 0.20, 2: 0.70}`.

## `dollar_per_unit` — the lot-sizing kernel (`strategy_common.py:13`)

Returns `k` such that `dollar_per_lot = price_distance × k`:

```
if ticker endswith "USD" and contract_size > 0:   k = contract_size      # P&L already in USD/unit
else:                                              k = tick_value / tick_size
```

So for an xxxUSD pair, dollars at distance D = `D × contract_size`. For a USDxxx pair, the broker
`tick_value` already does the foreign-currency conversion, so dollars = `(D / tick_size) × tick_value`.
All contract data (`contract_size`, `tick_size`, `tick_value`, `digits`) comes live from MT5 via
the ZMQ equity query — so the math generalizes to any pair without code change.

## Direction

`invert_signal(LONG)=SHORT`. Personal leg = the signal direction; prop leg = the inverse.

---

## Phase 2 geometry (`phase2_strategy.compute_geometry`)

Both personal SL and TP are **fixed from the signal**. Prop is the mirror hedge.

```
prop_dollar_risk = baseline_equity × prop_risk_pct          # = baseline × 1.0%
sl_distance      = |entry − signal_sl|                       # personal SL distance
tp_distance      = |signal_tp − entry|                       # = prop (funded) SL distance
                                                             # reject if tp_distance ≤ 0

prop_sl = signal_tp          # funded SL = signal TP (tight)
prop_tp = signal_sl          # funded TP = signal SL (wide)

prop_dollar_per_lot = tp_distance × k_prop                   # k from dollar_per_unit
prop_lots           = round(prop_dollar_risk / prop_dollar_per_lot, 2)

pers_lots           = round(prop_lots × phase_ratio, 2)      # phase_ratio = 0.70
pers_dollar_per_lot = sl_distance × k_pers
pers_dollar_risk    = round(pers_lots × pers_dollar_per_lot, 2)

pers_sl = signal_sl ; pers_tp = signal_tp
prop_signal = invert(signal) ; pers_signal = signal
```

Intuition: prop risks exactly `baseline × 1.0%` sized against the **tight** (signal-TP) distance;
when the signal's SL is hit, the prop wins big (wide leg). Personal is the smaller mirror at 70%.

## Phase 1 geometry (`phase1_strategy.compute_geometry`) — FIXED-LOT, moving-TP (rewritten 2026-06-07)

**Phase 1 ≠ Phase 2** (Warren, 2026-06-07). The signal is for PERSONAL; prop is the inverse.
**Only the signal TP (the near 1000-tick level) is used; the signal SL (3700t) is DISCARDED.**
The prop is sized over its own stop (= signal TP distance) so a stop-out loses exactly the fixed
per-trade risk → **lots are FIXED**. The growing stage gap is carried entirely by the **prop TP**,
which is calculated and **becomes the personal SL** (clean mirror box).

```
reward_gap = active_stage − live_prop_equity         # reject if ≤ 0 (await ratchet)
d_propSL   = |signal_tp − entry|                      # prop stop = signal TP dist ; reject if ≤ 0
                                                       # (signal_sl is accepted but UNUSED)
k_prop, k_pers = dollar_per_unit(...)                # reject if either ≤ 0

lots_prop = round(fixed_risk / (d_propSL × k_prop), 2)   # FIXED by risk; reject 0 / > max_prop_lots
lots_pers = round(lots_prop × pers_ratio, 2)             # pers_ratio = 0.20

prop_tp_dist = reward_gap / (lots_prop × k_prop)         # the ONLY thing that moves per-trade

# prop is inverse of signal:
if signal LONG  → prop SHORT: prop_tp = entry − prop_tp_dist
if signal SHORT → prop LONG : prop_tp = entry + prop_tp_dist
prop_sl = signal_tp          # prop stop = signal TP price        (NEAR barrier)
pers_sl = prop_tp            # personal SL = prop TP price         (FAR barrier — moves with gap)
pers_tp = prop_sl            # personal TP = prop SL = signal TP   (NEAR barrier)
```

All inputs are **formula/config-driven, nothing hardcoded**: `fixed_risk` + `max_prop_lots` from the
`phase1` block of `phase_config.json`; `k` live from MT5 contract data via `dollar_per_unit`;
`pers_ratio` from `risk_params.json`; `reward_gap` from the stage ladder − live equity.

**Behavior over a losing run:** lots stay **fixed** (e.g. gold $1,000 risk over the 1000-tick
signal-TP distance → 1.00 lot; $2,000 on a $100k baseline → 2.00 lot). The active stage ratchets
*up* only, so after a loss the gap grows (4,500 → 5,500 → 6,500) and is carried by **moving the
prop TP further out** → RR = `reward_gap / fixed_risk` climbs 4.5 → 5.5 → 6.5 (shrinks to ~0.25 on
later small-gap stages). The personal SL tracks the prop TP. This is the **"lots fixed, TP moves"**
scheme — the opposite of the pre-2026-06-07 "lots scale, TP fixed" model. Memory:
[[phase1-reward-risk-scaling]].

### Phase 1 stages (`phase1_strategy.derive_stages`)

Cumulative absolute prop-equity targets, set once at `/phase1` confirm:

```
target  = baseline × profit_target_pct / 100          # the funded line, in $
n       = min_profit_days                             # must be ≥ 2
step    = (target − first_reward) / (n − 1)
stages  = [ baseline + first_reward + step×i  for i in 0..n−1 ]
# stages[0]  = baseline + first_reward
# stages[-1] = baseline + target   (funded line → K4)
```

Validation (`validate_phase1_inputs`): reward/risk/baseline > 0; profit_target_pct > 0;
min_profit_days ≥ 2; **first_reward < target** (else no room for later stages).

`reward:risk` is entered via `/phase1` as a dollar pair `reward:risk` (e.g. `4500:1000`). It is the
**prop** perspective. The $50k account uses half the $100k figures (so `4500:1000`, not `9000:2000`).
Risk is **fixed per trade**; the realized reward:risk ratio shrinks across stages (≈4.5 → 0.25 by
design — NOT constant-RR). `min_days × per-trade-risk ≈ 6%` overall DD by construction. See memory
[[phase1-reward-risk-scaling]].

### Active-stage ratchet (`active_stage_index`)

Index of the lowest stage strictly greater than `current_equity`. **Ratchets only** — never returns
below `prev_index`. Returns `len(stages)` once the final stage is reached (caller treats that as K4 /
funded). Persisted in `phase_config.json → phase1.active_stage_index` via `state._phase1_active_stage`
(only advances are written). `state._phase1_record_stage_day` bumps `profitable_days` and stamps
`last_stage_day` once per prop-day.

---

## Kill conditions

Phase 1 kills are decided purely in `phase1_strategy.evaluate_kills`; Phase 2+ kills are inline in
`logic_core._run_equity_check` (`logic_core.py:890`). **All kills are PROP-only.** Detailed formulas:
`TECHNICAL.md §Kill Conditions`.

### Phase 1 (`evaluate_kills`, priority K2 > K1 > stage-win > K4)

| Kill | Condition | Permanent? |
|---|---|---|
| **K2** overall DD | `prop_equity ≤ baseline − baseline×dd_overall%/100` | **yes** (permanent halt) |
| **K1** daily loss | `prop_equity ≤ day_start − day_start×dd_daily%/100` | no — auto-resumes next session |
| **stage-win** | `prop_equity ≥ stages[active_index]` (not the last) | no — day halt; ratchet advances; counts a profitable day |
| **K4** profit target | `prop_equity ≥ stages[-1]` (funded line) | **yes** |

No K3 (profit cap) and no K5 (consistency) in Phase 1.

### Phase 2+ (`_run_equity_check`, phase ≠ 1)

| Kill | Level | Permanent? |
|---|---|---|
| **K2** overall DD | `prop_equity ≤ baseline − baseline×dd_overall%/100` | **yes** |
| **K1** daily DD | `prop_equity ≤ day_start − day_start×dd_daily%/100` (dynamic, resets each session) | no — day halt |
| **K3** daily profit cap | `prop_equity ≥ day_start + baseline×daily_profit_cap%/100` (protects K5; resets each session) | no — day halt |
| **K4** profit target | `(prop_equity − baseline)/baseline×100 ≥ profit_target%` | **yes** |
| **K5** consistency (phase 2 only) | largest single profitable day < `consistency_threshold%` of total profit (today's live P&L included) | **yes** |

A **day halt** sets `daily_halted` + `daily_halted_date`; it clears automatically when a new prop
session begins (auto-resume at `logic_core.py:778`). A **permanent halt** sets `permanently_halted`
and only `/phase2` (or manual edit) clears it. `/resume` sets a same-day `soft_kill_override_day`
that suppresses K1/K3/stage-halt **for the rest of that day**; `/rearm` clears that override so
soft kills fire again. Permanent kills (K2/K4/K5) ignore the override.

### Safety buffers (`state._apply_buffers`)

Applied to the **raw** prop-firm limits the operator entered, to fire **before** the firm's hard line:

```
max_drawdown_daily_pct    -= 1.0      # firm 3% → bot enforces 2%
max_drawdown_overall_pct   = raw      # NO buffer (firm closes at the exact %)
daily_profit_cap_pct       = profit_target_pct × 0.25   # 25% of target (vs the 30% consistency rule)
consistency_threshold_pct -= 1.0      # fire 1pp before the firm's limit
```

## Day boundary (`state._propfirm_day`)

The prop firm resets at a **configurable SGT time** — `propfirm_config.json` `propfirm_day_roll`
(`"HH:MM"`, default `11:00`), read live by `state._propfirm_roll_min()` and set on the running bot
with `/setdayroll HH:MM`. This is the firm's FIXED daily-loss reset (FundingPips "Resets In"), per
account — NOT a rolling 24h from the last trade. Any SGT time before the roll belongs to the trading
day that opened at the roll on the **previous** calendar day. Safety: erring late is safe; erring
early re-opens the daily allowance before the firm does (daily-DD breach risk). At rollover (`day_start_date_utc` changes), the
monitor: applies any scheduled `next_window`, locks the completed day's profit into the consistency
log (Phase 2), and resets `day_start_equity` (prop and personal). SGT helpers + curfew:
`TECHNICAL.md §Trading Window`.

## Where the dollar/RR display numbers come from

Geometry returns `prop_dollar_risk`, `pers_dollar_risk`, and (Phase 1 only) `prop_reward`,
`pers_reward`, `prop_rr`, `pers_rr`, `reward_gap`, `active_stage`. These are **display-only**
(computed from unrounded distances) and are passed to `msg_trade_opened`. They never feed back into
sizing.
