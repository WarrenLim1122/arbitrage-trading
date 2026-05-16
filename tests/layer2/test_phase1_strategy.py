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

# EURUSD: ends in "USD", contract_size 100000 -> k = 100000
_BASE = dict(
    ticker="EURUSD", signal="LONG", entry=1.08500, signal_sl=1.08300,
    price_digits=5,
    prop_contract_size=100000.0, prop_tick_size=0.00001, prop_tick_value=1.0,
    pers_contract_size=100000.0, pers_tick_size=0.00001, pers_tick_value=1.0,
    fixed_risk=2000.0, pers_ratio=0.20,
)


def test_geometry_first_trade():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0, **_BASE)
    assert "reject" not in g
    # D = 0.00200 ; lots_prop = 9000 / (0.002*100000) = 45.0
    assert g["prop_lots"] == 45.0
    assert g["prop_signal"] == "SHORT"           # inverse of LONG
    assert g["pers_signal"] == "LONG"            # follows signal
    assert g["prop_tp"] == 1.08300               # prop TP == signal SL price
    assert g["pers_sl"] == 1.08300               # personal SL == signal SL price
    # prop SL distance = D * R / reward = 0.002*2000/9000 = 0.000444444
    assert g["prop_sl"] == round(1.08500 + 0.000444444, 5)   # 1.08544
    assert g["pers_tp"] == g["prop_sl"]          # shared anchor
    assert g["prop_dollar_risk"] == pytest.approx(2000.0, abs=0.01)
    assert g["prop_reward"] == pytest.approx(9000.0, abs=0.01)
    assert g["pers_lots"] == 9.0                 # 45 * 0.20
    assert g["pers_dollar_risk"] == pytest.approx(1800.0, abs=0.01)  # 0.2*9000
    assert g["pers_reward"] == pytest.approx(400.0, abs=0.01)        # 0.2*2000
    assert g["prop_rr"] == pytest.approx(4.5, abs=0.001)


def test_geometry_harder_after_losses():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=98000.0, **_BASE)
    assert g["prop_lots"] == 55.0                # 11000 / 200
    assert g["prop_dollar_risk"] == pytest.approx(2000.0, abs=0.01)
    assert g["prop_reward"] == pytest.approx(11000.0, abs=0.01)
    assert g["pers_dollar_risk"] == pytest.approx(2200.0, abs=0.01)  # 0.2*11000
    assert g["pers_reward"] == pytest.approx(400.0, abs=0.01)


def test_geometry_short_signal_mirrors():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "signal": "SHORT", "signal_sl": 1.08700})
    # SHORT signal: signal_sl above entry; D = 0.00200
    assert g["prop_signal"] == "LONG"
    assert g["pers_signal"] == "SHORT"
    assert g["prop_tp"] == 1.08700               # prop TP == signal SL price
    assert g["pers_sl"] == 1.08700
    assert g["prop_sl"] == round(1.08500 - 0.000444444, 5)
    assert g["pers_tp"] == g["prop_sl"]


def test_geometry_rejects_zero_distance():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "signal_sl": 1.08500})
    assert "reject" in g


def test_geometry_rejects_nonpositive_reward():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=109000.0, **_BASE)
    assert "reject" in g


def test_geometry_rejects_lots_round_to_zero():
    # tiny reward gap + huge D -> lots < 0.005 -> rounds to 0.0
    g = compute_geometry(active_stage=100000.01, live_prop_equity=100000.0, **_BASE)
    assert "reject" in g


def test_geometry_rejects_over_max_lots():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          max_prop_lots=10.0, **_BASE)
    assert "reject" in g and "max" in g["reject"].lower()
