import pytest

from layer2.phase1_strategy import (
    parse_reward_risk,
    validate_phase1_inputs,
    derive_stages,
    active_stage_index,
)


def test_parse_reward_risk_ok():
    assert parse_reward_risk("9000:2000") == (9000.0, 2000.0)
    assert parse_reward_risk("  9000 : 2000 ") == (9000.0, 2000.0)
    assert parse_reward_risk("9000.5:2000") == (9000.5, 2000.0)


@pytest.mark.parametrize("bad", ["9000", "abc", "9000:0", "0:2000", "9000:-1", ":", "9000:2000:1"])
def test_parse_reward_risk_rejects(bad):
    with pytest.raises(ValueError):
        parse_reward_risk(bad)


def test_validate_ok():
    # baseline 100000, target 10% = 10000; W1 9000 < 10000; days 3
    assert validate_phase1_inputs(9000, 2000, 100000, 10.0, 3) is None


def test_validate_rejects_w1_ge_target():
    err = validate_phase1_inputs(10000, 2000, 100000, 10.0, 3)
    assert err is not None and "target" in err.lower()


def test_validate_rejects_min_days_lt_2():
    err = validate_phase1_inputs(9000, 2000, 100000, 10.0, 1)
    assert err is not None


def test_validate_rejects_nonpositive():
    assert validate_phase1_inputs(0, 2000, 100000, 10.0, 3) is not None
    assert validate_phase1_inputs(9000, 0, 100000, 10.0, 3) is not None
    assert validate_phase1_inputs(9000, 2000, 0, 10.0, 3) is not None


def test_derive_stages_three_days():
    assert derive_stages(100000, 9000, 10.0, 3) == [109000.0, 109500.0, 110000.0]


def test_derive_stages_four_days():
    s = derive_stages(100000, 9000, 10.0, 4)
    assert s[0] == 109000.0
    assert s[-1] == 110000.0
    assert len(s) == 4
    assert s[1] == pytest.approx(109333.33, abs=0.01)


def test_active_stage_index_start_and_ratchet():
    stages = [109000.0, 109500.0, 110000.0]
    assert active_stage_index(stages, 100000.0, 0) == 0          # aiming 109000
    assert active_stage_index(stages, 109000.0, 0) == 1          # reached S1 -> aim 109500
    # never reverts after a loss
    assert active_stage_index(stages, 107000.0, 1) == 1          # still aim 109500
    assert active_stage_index(stages, 109500.0, 1) == 2          # reached S2 -> aim 110000
    assert active_stage_index(stages, 110000.0, 2) == 3          # final reached (== len)


def test_active_stage_index_skips_overshoot():
    stages = [109000.0, 109500.0, 110000.0]
    assert active_stage_index(stages, 109800.0, 0) == 2          # jumped past S1+S2


def test_active_stage_index_init_above_first():
    stages = [109000.0, 109500.0, 110000.0]
    assert active_stage_index(stages, 109200.0, 0) == 1          # start already above S1


from layer2.phase1_strategy import compute_geometry

# EURUSD: ends in "USD", contract_size 100000 -> k = 100000.
# Fixed-lot / moving-TP mirror box: ONLY the signal TP (near) is used → prop SL.
# Prop sized over it (lots FIXED). Prop TP carries the stage gap and BECOMES the
# personal SL. The signal SL is DISCARDED.
#   LONG entry 1.08500, signal_tp 1.08600 (near, 0.00100). signal_sl is ignored.
_BASE = dict(
    ticker="EURUSD", signal="LONG", entry=1.08500,
    signal_sl=1.08300, signal_tp=1.08600,
    price_digits=5,
    prop_contract_size=100000.0, prop_tick_size=0.00001, prop_tick_value=1.0,
    pers_contract_size=100000.0, pers_tick_size=0.00001, pers_tick_value=1.0,
    fixed_risk=2000.0, pers_ratio=0.20,
)


def test_geometry_first_trade():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0, **_BASE)
    assert "reject" not in g
    # prop sized over signal-TP dist 0.00100: lots = 2000 / (0.001*100000) = 20.0
    assert g["prop_lots"] == 20.0
    assert g["prop_signal"] == "SHORT"           # inverse of LONG
    assert g["pers_signal"] == "LONG"            # follows signal
    assert g["prop_sl"] == 1.08600               # prop stop == signal TP (near)
    assert g["pers_tp"] == 1.08600               # personal TP == prop SL == signal TP (near)
    assert g["prop_dollar_risk"] == pytest.approx(2000.0, abs=0.01)   # == fixed_risk
    # gap 9000 carried by prop TP distance = 9000 / (20*100000) = 0.0045 -> TP below entry
    assert g["prop_tp"] == round(1.08500 - 0.0045, 5)                 # 1.08050
    assert g["pers_sl"] == g["prop_tp"]          # personal SL == prop TP (far barrier)
    assert g["prop_reward"] == pytest.approx(9000.0, abs=0.01)        # == reward_gap
    assert g["prop_rr"] == pytest.approx(4.5, abs=0.001)              # 9000/2000
    assert g["pers_lots"] == 4.0                 # 20 * 0.20
    # personal stop at the far barrier (prop TP dist 0.0045): 4*100000*0.0045 = 1800
    assert g["pers_dollar_risk"] == pytest.approx(1800.0, abs=0.01)
    assert g["pers_reward"] == pytest.approx(400.0, abs=0.01)         # 4*100000*0.001


def test_geometry_lots_fixed_tp_moves_after_loss():
    # After a loss the gap grows 9000 -> 11000; LOTS STAY 20, only the TP moves out
    # (and the personal SL moves with it).
    g = compute_geometry(active_stage=109000.0, live_prop_equity=98000.0, **_BASE)
    assert g["prop_lots"] == 20.0                # unchanged (fixed risk)
    assert g["prop_dollar_risk"] == pytest.approx(2000.0, abs=0.01)
    assert g["prop_reward"] == pytest.approx(11000.0, abs=0.01)       # == new gap
    assert g["prop_rr"] == pytest.approx(5.5, abs=0.001)              # 11000/2000
    assert g["prop_tp"] == round(1.08500 - 11000 / (20 * 100000), 5)  # TP further out
    assert g["pers_sl"] == g["prop_tp"]          # personal SL tracks prop TP


def test_geometry_short_signal_mirrors():
    # SHORT: signal_tp below entry (near). signal_sl is ignored.
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "signal": "SHORT",
                             "signal_sl": 1.08700, "signal_tp": 1.08400})
    assert g["prop_signal"] == "LONG"
    assert g["pers_signal"] == "SHORT"
    assert g["prop_sl"] == 1.08400               # prop stop == signal TP (near)
    assert g["pers_tp"] == 1.08400               # personal TP == signal TP
    assert g["prop_lots"] == 20.0                # signal-TP dist still 0.00100
    assert g["prop_tp"] == round(1.08500 + 9000 / (20 * 100000), 5)   # prop LONG TP above
    assert g["pers_sl"] == g["prop_tp"]          # personal SL == prop TP


def test_geometry_signal_sl_is_ignored():
    # Changing the signal SL must NOT change any output (it is discarded).
    g1 = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0, **_BASE)
    g2 = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "signal_sl": 1.05000})
    assert g1 == g2


def test_geometry_rejects_zero_tp_distance():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "signal_tp": 1.08500})
    assert "reject" in g


def test_geometry_rejects_nonpositive_gap():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=109000.0, **_BASE)
    assert "reject" in g


def test_geometry_rejects_lots_round_to_zero():
    # tiny risk + huge signal-TP distance -> lots < 0.005 -> rounds to 0.0
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "fixed_risk": 0.01, "signal_tp": 1.50000})
    assert "reject" in g


def test_geometry_rejects_over_max_lots():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          max_prop_lots=10.0, **_BASE)
    assert "reject" in g and "max" in g["reject"].lower()
