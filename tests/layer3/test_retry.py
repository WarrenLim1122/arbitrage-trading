"""Tests for layer3._retry — the pure market-order retry driver.

This module has no MetaTrader5/zmq imports so it runs on the dev machine.
The driver re-attempts a MARKET order while the broker reports the symbol
transiently closed (gold's daily settlement break), giving up only at the
end of the signal's 15m entry bar.
"""
from layer3._retry import run_market_retry, signal_bar_deadline_epoch


class FakeClock:
    """Deterministic clock: sleep() advances virtual time, no real waiting."""

    def __init__(self, start: float = 0.0):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_deadline_is_end_of_entry_bar():
    # timestamp_ms is the signal bar's OPEN; alert fires at +15m (bar close);
    # the trade enters on the next bar, which ends at +30m.
    assert signal_bar_deadline_epoch(1779133500000) == 1779133500 + 1800


def test_deadline_respects_custom_bar_length():
    assert signal_bar_deadline_epoch(1_000_000, bar_seconds=60) == 1000 + 120


def test_fills_on_first_attempt_no_retry():
    clock = FakeClock()
    filled = {"status": "FILLED", "mt5_order_ticket": 42}
    calls = []

    def attempt():
        calls.append(1)
        return ("filled", filled)

    result = run_market_retry(
        deadline_epoch=1000, attempt=attempt,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "FILLED"
    assert result["mt5_order_ticket"] == 42
    assert len(calls) == 1
    assert clock.sleeps == []


def test_retries_until_market_reopens_then_fills():
    clock = FakeClock()
    outcomes = [
        ("retry", "Market closed", 10018),
        ("retry", "Market closed", 10018),
        ("filled", {"status": "FILLED", "mt5_order_ticket": 99}),
    ]

    def attempt():
        return outcomes.pop(0)

    result = run_market_retry(
        deadline_epoch=1000, attempt=attempt,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "FILLED"
    assert result["mt5_order_ticket"] == 99
    assert clock.sleeps == [15.0, 15.0]  # slept between the two failed tries


def test_gives_up_at_deadline_with_last_broker_reason():
    clock = FakeClock()

    def attempt():
        return ("retry", "Market closed", 10018)

    result = run_market_retry(
        deadline_epoch=40, attempt=attempt,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "REJECTED"
    assert result["broker_comment"] == "Market closed"
    assert result["broker_retcode"] == "10018"


def test_deadline_already_passed_makes_no_attempt():
    clock = FakeClock(start=100.0)
    calls = []

    def attempt():
        calls.append(1)
        return ("retry", "Market closed", 10018)

    result = run_market_retry(
        deadline_epoch=50, attempt=attempt,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "REJECTED"
    assert calls == []
    assert clock.sleeps == []


def test_aborts_mid_retry_and_stops_attempting():
    clock = FakeClock()
    calls = []
    abort_calls = []

    def attempt():
        calls.append(1)
        return ("retry", "Market closed", 10018)

    def should_abort():
        abort_calls.append(1)
        return None if len(abort_calls) == 1 else "curfew"

    result = run_market_retry(
        deadline_epoch=10_000, attempt=attempt, should_abort=should_abort,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "REJECTED"
    assert "aborted" in result["broker_comment"]
    assert "curfew" in result["broker_comment"]
    assert len(calls) == 1  # no further attempts after abort fired


def test_fatal_rejection_is_not_retried():
    clock = FakeClock()
    calls = []

    def attempt():
        calls.append(1)
        return ("fatal", "No money", 10019)

    result = run_market_retry(
        deadline_epoch=10_000, attempt=attempt,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "REJECTED"
    assert result["broker_comment"] == "No money"
    assert result["broker_retcode"] == "10019"
    assert len(calls) == 1
    assert clock.sleeps == []
