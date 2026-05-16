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
