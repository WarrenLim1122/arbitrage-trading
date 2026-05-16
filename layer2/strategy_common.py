"""Pure helpers shared by phase strategy modules.

No imports from layer2.state / layer2.logic_core — keep this side-effect free
and unit-testable.
"""


def invert_signal(signal: str) -> str:
    """LONG <-> SHORT."""
    return "SHORT" if signal == "LONG" else "LONG"


def dollar_per_unit(
    ticker: str,
    contract_size: float,
    tick_size: float,
    tick_value: float,
) -> float:
    """Return k such that dollar_per_lot at price-distance X == X * k.

    Mirrors the existing Layer 2 rule (logic_core.receive_signal):
      - xxxUSD pair with a contract size → P&L is USD/unit → k = contract_size
        (dollar_per_lot = distance * contract_size)
      - otherwise → use broker tick data → k = tick_value / tick_size
        (dollar_per_lot = (distance / tick_size) * tick_value)
    """
    if ticker.endswith("USD") and contract_size > 0:
        return contract_size
    return tick_value / tick_size
