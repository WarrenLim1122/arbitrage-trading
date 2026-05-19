"""Tests for layer3._retry — the pure market-order retry driver.

This module has no MetaTrader5/zmq imports so it runs on the dev machine.
The driver re-attempts a MARKET order while the broker reports the symbol
transiently closed (gold's daily settlement break). On the worker side a
1-minute / 15s-interval window is used; when it is exhausted the worker drops
a resting LIMIT order at the signal entry. The driver tags every non-fill
outcome with a ``reason`` so the worker can tell "deadline" (place the limit
fallback) apart from "aborted" (curfew/kill — do nothing).
"""
from layer3._retry import run_market_retry


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


def test_four_attempts_over_a_one_minute_window():
    # Worker uses deadline = now + 60, interval = 15  ->  4 attempts.
    clock = FakeClock()
    calls = []

    def attempt():
        calls.append(1)
        return ("retry", "Market closed", 10018)

    result = run_market_retry(
        deadline_epoch=60, attempt=attempt,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "REJECTED"
    assert result["reason"] == "deadline"
    assert len(calls) == 4
    assert clock.sleeps == [15.0, 15.0, 15.0, 15.0]


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
    assert clock.sleeps == [15.0, 15.0]


def test_deadline_outcome_carries_reason_and_last_broker_reason():
    clock = FakeClock()

    def attempt():
        return ("retry", "Market closed", 10018)

    result = run_market_retry(
        deadline_epoch=40, attempt=attempt,
        now=clock.now, sleep=clock.sleep, interval=15.0,
    )

    assert result["status"] == "REJECTED"
    assert result["reason"] == "deadline"
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
    assert result["reason"] == "deadline"
    assert calls == []
    assert clock.sleeps == []


def test_aborts_mid_retry_with_reason_aborted():
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
    assert result["reason"] == "aborted"
    assert "curfew" in result["broker_comment"]
    assert len(calls) == 1  # no further attempts after abort fired


def test_fatal_rejection_is_not_retried_and_tagged_fatal():
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
    assert result["reason"] == "fatal"
    assert result["broker_comment"] == "No money"
    assert result["broker_retcode"] == "10019"
    assert len(calls) == 1
    assert clock.sleeps == []
