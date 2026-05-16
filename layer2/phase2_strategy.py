"""Phase 2 — verbatim extraction of the current Layer 2 geometry (pure).

Math is byte-identical to logic_core.receive_signal lines 1418-1511 as of
commit 56c86bf. Do not "improve" it — the regression test pins exact numbers.
"""
from __future__ import annotations

from layer2.strategy_common import invert_signal


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
    baseline_equity: float,
    prop_risk_pct: float,
    phase_ratio: float,
) -> dict:
    """Exact current behaviour. Returns ticket fields or {"reject": reason}."""
    prop_dollar_risk = baseline_equity * prop_risk_pct

    sl_distance = abs(entry - signal_sl)     # personal SL distance (signal perspective)
    tp_distance = abs(signal_tp - entry)     # funded SL distance = signal TP distance

    if tp_distance <= 0:
        return {"reject": f"TP distance is zero (tp={signal_tp} entry={entry})"}

    # Funded SL = signal TP (tight) ; Funded TP = signal SL (wide)
    prop_sl = round(signal_tp, price_digits)
    prop_tp = round(signal_sl, price_digits)

    if ticker.endswith("USD") and prop_contract_size > 0:
        prop_dollar_per_lot = tp_distance * prop_contract_size
    else:
        prop_dollar_per_lot = (tp_distance / prop_tick_size) * prop_tick_value
    prop_lots = round(prop_dollar_risk / prop_dollar_per_lot, 2)

    pers_lots = round(prop_lots * phase_ratio, 2)
    if ticker.endswith("USD") and pers_contract_size > 0:
        pers_dollar_per_lot = sl_distance * pers_contract_size
    else:
        pers_dollar_per_lot = (sl_distance / pers_tick_size) * pers_tick_value
    pers_dollar_risk = round(pers_lots * pers_dollar_per_lot, 2)

    pers_tp = round(signal_tp, price_digits)   # personal TP = signal TP

    return {
        "prop_signal": invert_signal(signal),
        "prop_lots": prop_lots,
        "prop_sl": prop_sl,
        "prop_tp": prop_tp,
        "prop_dollar_risk": round(prop_dollar_risk, 2),
        "pers_signal": signal,
        "pers_lots": pers_lots,
        "pers_sl": round(signal_sl, price_digits),
        "pers_tp": pers_tp,
        "pers_dollar_risk": pers_dollar_risk,
        "sl_distance": sl_distance,
        "tp_distance": tp_distance,
    }
