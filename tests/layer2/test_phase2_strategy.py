import pytest

from layer2.phase2_strategy import compute_geometry

# Reproduces the CURRENT logic_core math for a known signal. EURUSD ends in
# "USD", contract_size 100000. prop_risk_pct raised 0.67% -> 1.0% (2026-06-14).
#   prop_dollar_risk = baseline * 0.01 = 100000 * 0.01 = 1000.0
#   sl_distance = |entry - sl| = |1.08500 - 1.08300| = 0.00200
#   tp_distance = |tp - entry| = |1.08554 - 1.08500| = 0.00054
#   prop_dollar_per_lot = tp_distance * contract_size = 0.00054 * 100000 = 54.0
#   prop_lots = round(1000 / 54, 2) = 18.52
#   pers_lots = round(18.52 * 0.70, 2) = 12.96
#   pers_dollar_per_lot = sl_distance * contract_size = 0.00200 * 100000 = 200.0
#   pers_dollar_risk = round(12.96 * 200.0, 2) = 2592.0
#   prop_sl = round(tp,5)=1.08554 ; prop_tp = round(sl,5)=1.08300
#   pers_tp = round(tp,5)=1.08554
def test_phase2_geometry_matches_current_formula():
    g = compute_geometry(
        ticker="EURUSD", signal="LONG",
        entry=1.08500, signal_sl=1.08300, signal_tp=1.08554,
        price_digits=5,
        prop_contract_size=100000.0, prop_tick_size=0.00001, prop_tick_value=1.0,
        pers_contract_size=100000.0, pers_tick_size=0.00001, pers_tick_value=1.0,
        baseline_equity=100000.0, prop_risk_pct=0.01, phase_ratio=0.70,
    )
    assert g["prop_lots"] == 18.52
    assert g["pers_lots"] == 12.96
    assert g["prop_dollar_risk"] == pytest.approx(1000.0, abs=0.01)
    assert g["pers_dollar_risk"] == pytest.approx(2592.0, abs=0.01)
    assert g["prop_sl"] == 1.08554          # funded SL = signal TP
    assert g["prop_tp"] == 1.08300          # funded TP = signal SL
    assert g["pers_sl"] == 1.08300          # personal uses webhook sl
    assert g["pers_tp"] == 1.08554          # personal TP = signal TP
    assert g["prop_signal"] == "SHORT"
    assert g["pers_signal"] == "LONG"


def test_phase2_geometry_rejects_zero_tp_distance():
    g = compute_geometry(
        ticker="EURUSD", signal="LONG",
        entry=1.08500, signal_sl=1.08300, signal_tp=1.08500,
        price_digits=5,
        prop_contract_size=100000.0, prop_tick_size=0.00001, prop_tick_value=1.0,
        pers_contract_size=100000.0, pers_tick_size=0.00001, pers_tick_value=1.0,
        baseline_equity=100000.0, prop_risk_pct=0.01, phase_ratio=0.70,
    )
    assert "reject" in g
