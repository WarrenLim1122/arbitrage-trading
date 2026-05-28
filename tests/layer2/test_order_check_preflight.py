"""Pre-flight 'Signal Not Placed' per-leg messaging (Issue 2).

When one leg fails the order_check, Layer 2 places nothing and reports why.
_order_check_leg_line turns each worker verdict into a human-readable status line.
"""
from layer2.telegram_handlers import _msg_order_check_leg_line as _order_check_leg_line


def test_ok_leg():
    line = _order_check_leg_line("Prop Hedge", {"verdict": "ok"})
    assert "Prop Hedge" in line
    assert "Can fill" in line


def test_transient_leg_shows_comment():
    line = _order_check_leg_line(
        "Personal Signal", {"verdict": "transient", "comment": "Market closed"})
    assert "Market closed" in line
    assert "⚠️" in line


def test_reject_no_money_shows_margin_detail():
    line = _order_check_leg_line("Personal Signal", {
        "verdict": "reject", "retcode": 10019,
        "margin": 12000.0, "margin_free": -2000.0, "comment": "Not enough money",
    })
    assert "REJECTED" in line
    assert "12,000.00" in line   # margin needed surfaced
    assert "-$2,000.00" in line  # free margin surfaced (signed money)


def test_reject_negative_free_margin_without_code():
    line = _order_check_leg_line("Personal Signal", {
        "verdict": "reject", "retcode": 10006, "margin_free": -5.0, "margin": 100.0,
    })
    # Negative free margin should still surface an insufficient-funds detail
    assert "REJECTED" in line
    assert "-$5.00" in line


def test_reject_invalid_stops_mapped():
    line = _order_check_leg_line("Prop Hedge", {
        "verdict": "reject", "retcode": 10016, "comment": "Invalid stops",
    })
    assert "Invalid stops" in line


def test_reject_unknown_falls_back_to_comment():
    line = _order_check_leg_line("Prop Hedge", {
        "verdict": "reject", "retcode": 99999, "comment": "weird broker error",
    })
    assert "weird broker error" in line
