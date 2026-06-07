"""Phase 1 — fixed-lot, moving-TP strategy (pure).

Phase 1 is DIFFERENT from Phase 2 (see docs/reference/calculations.md): only the
signal TP is used (signal SL discarded); the prop is sized over its own stop so
lots are FIXED per trade, and the prop TP is calculated to win the stage gap and
becomes the personal SL. See `compute_geometry` for the full workflow.

No imports from layer2.state / layer2.logic_core. All inputs are primitives;
outputs are plain dicts.
"""
from __future__ import annotations

from layer2.strategy_common import invert_signal, dollar_per_unit


def parse_reward_risk(text: str) -> tuple[float, float]:
    """Parse a 'reward:risk' dollar pair, e.g. '9000:2000' -> (9000.0, 2000.0).

    Raises ValueError on any malformed / non-positive input.
    """
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError("expected exactly one ':' (format reward:risk)")
    try:
        reward = float(parts[0].strip())
        risk = float(parts[1].strip())
    except ValueError:
        raise ValueError("reward and risk must be numbers")
    if reward <= 0 or risk <= 0:
        raise ValueError("reward and risk must be positive")
    return reward, risk


def validate_phase1_inputs(
    first_reward: float,
    fixed_risk: float,
    baseline: float,
    profit_target_pct: float,
    min_profit_days: int,
) -> str | None:
    """Return an error string if inputs are unusable, else None."""
    if first_reward <= 0 or fixed_risk <= 0 or baseline <= 0:
        return "Reward, risk and baseline must all be positive."
    if profit_target_pct <= 0:
        return "Prop-firm profit target % is not set — run /changepropfirm first."
    if min_profit_days < 2:
        return ("Prop-firm min profitable days must be ≥ 2 "
                "(need Stage 1 + the funded line). Set it via /changepropfirm.")
    target = baseline * profit_target_pct / 100.0
    if first_reward >= target:
        return (f"First reward ${first_reward:,.0f} must be LESS than the overall "
                f"target ${target:,.0f} (else there is no room for later stages).")
    return None


def derive_stages(
    baseline: float,
    first_reward: float,
    profit_target_pct: float,
    min_profit_days: int,
) -> list[float]:
    """Cumulative absolute prop-equity targets.

    stages[0]  = baseline + first_reward
    stages[-1] = baseline + (baseline * profit_target_pct/100)   (funded line)
    Intermediate stages split (target - first_reward) evenly over (n-1) days.
    """
    target = baseline * profit_target_pct / 100.0
    n = int(min_profit_days)
    step = (target - first_reward) / (n - 1)
    return [round(baseline + first_reward + step * i, 2) for i in range(n)]


def active_stage_index(stages: list[float], current_equity: float, prev_index: int) -> int:
    """Index of the lowest stage strictly greater than current_equity.

    Ratchets only — never returns below prev_index. Returns len(stages) when the
    final stage has been reached (caller treats that as K4 / funded).
    """
    idx = max(0, int(prev_index))
    while idx < len(stages) and current_equity >= stages[idx]:
        idx += 1
    return idx


def compute_geometry(
    *,
    ticker: str,
    signal: str,
    entry: float,
    signal_sl: float,
    signal_tp: float,
    price_digits: int,
    prop_contract_size: float,
    prop_tick_size: float,
    prop_tick_value: float,
    pers_contract_size: float,
    pers_tick_size: float,
    pers_tick_value: float,
    active_stage: float,
    live_prop_equity: float,
    fixed_risk: float,
    pers_ratio: float,
    max_prop_lots: float = 0.0,
) -> dict:
    """Phase 1 geometry — FIXED-LOT, moving-TP (lots fixed by risk; TP carries the stage gap).

    Workflow (the signal is for PERSONAL; prop is the inverse):
      1. Only the signal **TP** (the near 1000-tick level) is used; the signal **SL**
         (3700t) is DISCARDED.
      2. prop SL  = signal TP price (the prop's stop = NEAR barrier).
      3. lots_prop = fixed_risk / (|signal_tp − entry| × k_prop)  → sized over the
         prop's stop, so a stop-out loses exactly `fixed_risk`. **Lots are FIXED**
         per trade (independent of equity / stage gap).
      4. prop TP = CALCULATED to win the stage gap → distance
         `reward_gap / (lots_prop × k_prop)` (the FAR barrier; the only thing that
         moves trade-to-trade). RR = reward_gap / fixed_risk → 4.5 → 5.5 → 6.5 over a
         losing run, shrinking on later small-gap stages.
      5. personal SL = prop TP price (FAR barrier); personal TP = prop SL = signal TP
         (NEAR barrier). The two legs share both barriers (clean mirror box).
      6. lots_pers = pers_ratio × lots_prop.

    `signal_sl` is accepted for call-signature symmetry but intentionally UNUSED.
    Returns a dict of ticket fields, or {"reject": "<reason>"}.
    """
    reward_gap = round(active_stage - live_prop_equity, 2)
    if reward_gap <= 0:
        return {"reject": "equity at/above active stage — awaiting ratchet"}

    d_prop_sl = abs(signal_tp - entry)   # prop SL distance = signal TP distance (near, sizing)
    if d_prop_sl <= 0:
        return {"reject": f"signal TP distance is zero (entry={entry} tp={signal_tp})"}

    prop_k = dollar_per_unit(ticker, prop_contract_size, prop_tick_size, prop_tick_value)
    pers_k = dollar_per_unit(ticker, pers_contract_size, pers_tick_size, pers_tick_value)
    if prop_k <= 0 or pers_k <= 0:
        return {"reject": "invalid contract data (dollar-per-unit <= 0)"}

    # PROP sized over its own stop (signal-TP distance) → a stop-out loses fixed_risk.
    # Lots are FIXED per trade (do not scale with the stage gap).
    lots_prop = round(fixed_risk / (d_prop_sl * prop_k), 2)
    if lots_prop <= 0:
        return {"reject": "computed prop lots rounds to 0 (risk too small for TP distance)"}
    if max_prop_lots > 0 and lots_prop > max_prop_lots:
        return {"reject": f"computed prop lots {lots_prop:.2f} exceed max {max_prop_lots:.2f}"}

    lots_pers = round(lots_prop * pers_ratio, 2)
    if lots_pers <= 0:
        return {"reject": "computed personal lots rounds to 0"}

    # Prop TP distance carries the stage gap (the only part that moves trade-to-trade).
    prop_tp_dist = reward_gap / (lots_prop * prop_k)

    prop_signal = invert_signal(signal)
    if signal == "LONG":
        # prop SHORT: profits DOWN. TP below entry; SL above entry (= signal TP, near).
        prop_tp_price = entry - prop_tp_dist
    else:
        # signal SHORT -> prop LONG: profits UP. TP above entry; SL below (= signal TP).
        prop_tp_price = entry + prop_tp_dist

    prop_tp = round(prop_tp_price, price_digits)
    prop_sl = round(signal_tp, price_digits)   # prop stop == signal TP price (near barrier)
    pers_sl = prop_tp                          # personal SL == prop TP price (far barrier)
    pers_tp = prop_sl                          # personal TP == prop SL == signal TP (near)

    # Dollar figures from UNROUNDED distances (display/alert only).
    prop_dollar_risk = round(lots_prop * prop_k * d_prop_sl, 2)     # == fixed_risk
    prop_reward = round(lots_prop * prop_k * prop_tp_dist, 2)       # == reward_gap
    pers_dollar_risk = round(lots_pers * pers_k * prop_tp_dist, 2)  # personal stop at far barrier
    pers_reward = round(lots_pers * pers_k * d_prop_sl, 2)          # personal TP at near barrier
    prop_rr = prop_reward / prop_dollar_risk if prop_dollar_risk > 0 else 0.0
    pers_rr = pers_reward / pers_dollar_risk if pers_dollar_risk > 0 else 0.0

    return {
        "prop_signal": prop_signal,
        "prop_lots": lots_prop,
        "prop_sl": prop_sl,
        "prop_tp": prop_tp,
        "prop_dollar_risk": prop_dollar_risk,
        "prop_reward": prop_reward,
        "prop_rr": round(prop_rr, 4),
        "pers_signal": signal,
        "pers_lots": lots_pers,
        "pers_sl": pers_sl,
        "pers_tp": pers_tp,
        "pers_dollar_risk": pers_dollar_risk,
        "pers_reward": pers_reward,
        "pers_rr": round(pers_rr, 4),
        "sl_distance": round(prop_tp_dist, price_digits),   # personal SL dist (= far barrier)
        "tp_distance": round(d_prop_sl, price_digits),      # personal TP dist (= near barrier)
        "active_stage": active_stage,
        "reward_gap": reward_gap,
    }


def evaluate_kills(
    *,
    prop_equity: float,
    baseline: float,
    day_start: float,
    dd_daily_pct: float,
    dd_overall_pct: float,
    stages: list[float],
    active_index: int,
) -> dict | None:
    """Phase 1 kill decision (pure). Priority: K2 > K1 > stage-win > K4.

    Returns None or {reason, permanent, stage_value?}.
      - overall_drawdown_limit (K2)  permanent
      - daily_loss_limit       (K1)  not permanent (auto-resume next session)
      - phase1_stage_reached         not permanent (day halt; ratchet advances)
      - profit_target          (K4)  permanent (final stage reached)
    No K3 (daily profit cap) and no K5 (consistency) in Phase 1.
    """
    # K2 — static overall floor (permanent)
    if dd_overall_pct > 0 and baseline > 0:
        overall_floor = baseline - round(baseline * dd_overall_pct / 100.0, 2)
        if prop_equity <= overall_floor:
            return {"reason": "overall_drawdown_limit", "permanent": True,
                    "overall_floor": overall_floor}

    # K1 — dynamic daily floor (not permanent)
    if dd_daily_pct > 0 and day_start > 0:
        daily_floor = day_start - round(day_start * dd_daily_pct / 100.0, 2)
        if prop_equity <= daily_floor:
            return {"reason": "daily_loss_limit", "permanent": False,
                    "daily_floor": daily_floor}

    if not stages:
        return None

    # K4 — final stage (funded line) reached -> permanent
    if prop_equity >= stages[-1]:
        return {"reason": "profit_target", "permanent": True,
                "stage_value": stages[-1]}

    # Stage-win day-halt — reached the active stage (not the final one)
    if 0 <= active_index < len(stages) and prop_equity >= stages[active_index]:
        return {"reason": "phase1_stage_reached", "permanent": False,
                "stage_value": stages[active_index]}

    return None
