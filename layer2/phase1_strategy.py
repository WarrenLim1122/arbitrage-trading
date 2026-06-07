"""Phase 1 — dynamic reward-targeting strategy (pure).

No imports from layer2.state / layer2.logic_core. All inputs are primitives;
outputs are plain dicts. See docs/superpowers/specs/2026-05-16-phase1-strategy-design.md
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
    fixed_risk: float,
    pers_ratio: float,
    max_prop_lots: float = 0.0,
) -> dict:
    """Phase 1 geometry — fixed-risk "box" (identical model to Phase 2).

    Sizing starts from the PROP and a fixed per-trade dollar risk:
      - prop stop sits at the signal TP price (the near barrier);
        lots_prop = fixed_risk / (tp_distance * k)  → a stop-out loses fixed_risk.
      - prop target sits at the signal SL price (the far barrier).
      - personal takes the signal direction at the signal's own SL/TP;
        lots_personal = pers_ratio * lots_prop  (derived from prop).

    Shared box (direction-agnostic by price):
      prop TP / personal SL = signal SL price (far)
      prop SL / personal TP = signal TP price (near, = the prop's stop)

    The only difference from Phase 2 is the risk source: Phase 1 uses the typed
    fixed_risk; Phase 2 uses baseline * 0.67%. Returns ticket fields or
    {"reject": "<reason>"}.
    """
    d_sl = abs(entry - signal_sl)   # far barrier — prop TP / personal SL distance
    d_tp = abs(signal_tp - entry)   # near barrier — prop SL / personal TP distance
    if d_tp <= 0:
        return {"reject": f"signal TP distance is zero (entry={entry} tp={signal_tp})"}
    if d_sl <= 0:
        return {"reject": f"signal SL distance is zero (entry={entry} sl={signal_sl})"}

    prop_k = dollar_per_unit(ticker, prop_contract_size, prop_tick_size, prop_tick_value)
    pers_k = dollar_per_unit(ticker, pers_contract_size, pers_tick_size, pers_tick_value)
    if prop_k <= 0 or pers_k <= 0:
        return {"reject": "invalid contract data (dollar-per-unit <= 0)"}

    # PROP sized FIRST: its stop is the signal-TP distance, sized so a stop-out
    # loses exactly fixed_risk.  Personal lots are then DERIVED from prop.
    lots_prop = round(fixed_risk / (d_tp * prop_k), 2)
    if lots_prop <= 0:
        return {"reject": "computed prop lots rounds to 0 (risk too small for TP distance)"}
    if max_prop_lots > 0 and lots_prop > max_prop_lots:
        return {"reject": f"computed prop lots {lots_prop:.2f} exceed max {max_prop_lots:.2f}"}

    lots_pers = round(lots_prop * pers_ratio, 2)
    if lots_pers <= 0:
        return {"reject": "computed personal lots rounds to 0"}

    prop_signal = invert_signal(signal)
    # Shared barriers — assigned by price, so this works for LONG and SHORT alike.
    prop_tp = round(signal_sl, price_digits)   # prop target  == signal SL (far)
    prop_sl = round(signal_tp, price_digits)   # prop stop    == signal TP (near)
    pers_sl = round(signal_sl, price_digits)   # personal SL  == signal SL (far)
    pers_tp = round(signal_tp, price_digits)   # personal TP  == signal TP (near)

    # Dollar figures from UNROUNDED distances (display/alert only).
    prop_dollar_risk = round(lots_prop * prop_k * d_tp, 2)   # == fixed_risk
    prop_reward      = round(lots_prop * prop_k * d_sl, 2)
    pers_dollar_risk = round(lots_pers * pers_k * d_sl, 2)   # personal stop at far barrier
    pers_reward      = round(lots_pers * pers_k * d_tp, 2)
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
        "sl_distance": round(d_sl, price_digits),
        "tp_distance": round(d_tp, price_digits),
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
