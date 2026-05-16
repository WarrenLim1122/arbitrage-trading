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
    """Phase 1 geometry.

    Anchor = signal SL price = personal SL = prop TP.
    Prop SL & personal TP are computed (shared mirror price).
    lots_personal = pers_ratio * lots_prop.

    Returns a dict of ticket fields, or {"reject": "<reason>"}.
    """
    reward_prop = round(active_stage - live_prop_equity, 2)
    if reward_prop <= 0:
        return {"reject": "equity at/above active stage — awaiting ratchet"}

    d = abs(entry - signal_sl)
    if d <= 0:
        return {"reject": f"signal SL distance is zero (entry={entry} sl={signal_sl})"}

    prop_k = dollar_per_unit(ticker, prop_contract_size, prop_tick_size, prop_tick_value)
    pers_k = dollar_per_unit(ticker, pers_contract_size, pers_tick_size, pers_tick_value)
    if prop_k <= 0 or pers_k <= 0:
        return {"reject": "invalid contract data (dollar-per-unit <= 0)"}

    # Prop TP anchored at the signal-SL distance D → this lot size makes the
    # prop win exactly the stage gap.
    lots_prop = round(reward_prop / (d * prop_k), 2)
    if lots_prop <= 0:
        return {"reject": "computed prop lots rounds to 0 (reward gap too small for SL distance)"}
    if max_prop_lots > 0 and lots_prop > max_prop_lots:
        return {"reject": f"computed prop lots {lots_prop:.2f} exceed max {max_prop_lots:.2f}"}

    # Prop SL distance sized so a prop loss = exactly fixed_risk.
    prop_sl_dist = fixed_risk / (lots_prop * prop_k)

    lots_pers = round(lots_prop * pers_ratio, 2)
    if lots_pers <= 0:
        return {"reject": "computed personal lots rounds to 0"}

    prop_signal = invert_signal(signal)
    if signal == "LONG":
        # prop SHORT: TP below entry (= signal SL), SL above entry
        prop_tp_price = entry - d
        prop_sl_price = entry + prop_sl_dist
    else:
        # signal SHORT -> prop LONG: TP above entry (= signal SL), SL below entry
        prop_tp_price = entry + d
        prop_sl_price = entry - prop_sl_dist

    prop_tp = round(prop_tp_price, price_digits)
    prop_sl = round(prop_sl_price, price_digits)
    pers_sl = round(signal_sl, price_digits)   # personal SL == signal SL price
    pers_tp = prop_sl                          # personal TP == prop SL price (shared)

    # Dollar figures from UNROUNDED distances (display/alert only).
    prop_dollar_risk = round(lots_prop * prop_k * prop_sl_dist, 2)
    prop_reward = round(lots_prop * prop_k * d, 2)
    pers_dollar_risk = round(lots_pers * pers_k * d, 2)
    pers_reward = round(lots_pers * pers_k * prop_sl_dist, 2)
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
        "sl_distance": round(d, price_digits),
        "tp_distance": round(prop_sl_dist, price_digits),
        "active_stage": active_stage,
        "reward_gap": reward_prop,
    }
