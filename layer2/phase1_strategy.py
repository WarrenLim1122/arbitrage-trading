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
