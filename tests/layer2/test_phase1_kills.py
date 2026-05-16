import pytest
from layer2.phase1_strategy import evaluate_kills


_CFG = dict(baseline=100000.0, day_start=100000.0,
            dd_daily_pct=2.5, dd_overall_pct=6.0)


def test_k2_overall_floor():
    r = evaluate_kills(prop_equity=93999.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r["reason"] == "overall_drawdown_limit"
    assert r["permanent"] is True


def test_k4_final_stage_profit_target():
    r = evaluate_kills(prop_equity=110000.0, stages=[109000, 109500, 110000],
                       active_index=2, **_CFG)
    assert r["reason"] == "profit_target"
    assert r["permanent"] is True


def test_k1_daily_loss():
    # day_start 100000, 2.5% -> floor 97500
    r = evaluate_kills(prop_equity=97400.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r["reason"] == "daily_loss_limit"
    assert r["permanent"] is False


def test_stage_win_day_halt():
    r = evaluate_kills(prop_equity=109000.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r["reason"] == "phase1_stage_reached"
    assert r["permanent"] is False
    assert r["stage_value"] == 109000


def test_no_k3_daily_profit_cap():
    # well above day_start but below the stage -> nothing fires (no K3 in phase 1)
    r = evaluate_kills(prop_equity=108000.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r is None
