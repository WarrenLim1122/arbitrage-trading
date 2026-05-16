from layer2.state import _apply_buffers, _p2_display


def test_daily_dd_buffer_is_half_point():
    raw = {
        "max_drawdown_daily_pct": 3.0,
        "max_drawdown_overall_pct": 6.0,
        "profit_target_pct": 10.0,
        "consistency_threshold_pct": 30.0,
    }
    eff = _apply_buffers(raw)
    assert eff["max_drawdown_daily_pct"] == 2.5          # 3.0 - 0.5
    assert eff["max_drawdown_overall_pct"] == 6.0        # unbuffered
    assert eff["daily_profit_cap_pct"] == 2.5            # 25% of 10
    assert eff["consistency_threshold_pct"] == 29.0      # still -1.0pp


def test_p2_display_daily_shows_half_point():
    assert _p2_display("max_drawdown_daily_pct", 3.0) == \
        "3.0% (enforced at 2.5% after −0.5pp buffer)"
