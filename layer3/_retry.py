"""Pure market-order retry driver — no MetaTrader5 / zmq imports.

Gold (XAUUSD) closes for a short daily settlement/maintenance break. A MARKET
order fired during that window is rejected by the broker with retcode
``TRADE_RETCODE_MARKET_CLOSED`` ("Market closed"). Treating that single
rejection as terminal silently drops the signal — so the system never actually
enters the trade the user expects under a 24-hour trading window.

This driver re-attempts the same market order while the broker keeps reporting
the symbol transiently closed. The caller (the worker) bounds it to a short
window (1 minute / 15s interval ⇒ 4 tries); when that is exhausted the worker
falls back to a resting LIMIT order at the signal entry. Every non-fill outcome
is tagged with ``reason`` so the worker can tell the cases apart:

    "deadline" — window elapsed, broker still closed  → place the LIMIT fallback
    "aborted"  — curfew / kill-switch / news arrived   → do nothing
    "fatal"    — hard rejection (no money, bad stops)   → do nothing

Kept import-free so it is unit-testable on the dev machine, where
``MetaTrader5`` is unavailable.
"""
from typing import Callable


def _rejected(comment: str, retcode, attempts: int, reason: str) -> dict:
    return {
        "status":         "REJECTED",
        "reason":         reason,
        "broker_comment": comment,
        "broker_retcode": None if retcode is None else str(retcode),
        "retry_attempts": attempts,
    }


def run_market_retry(
    *,
    deadline_epoch: float,
    attempt: Callable[[], tuple],
    now: Callable[[], float],
    sleep: Callable[[float], None],
    should_abort: Callable[[], "str | None"] | None = None,
    interval: float = 15.0,
) -> dict:
    """Re-attempt a market order until it fills, is aborted, or time runs out.

    ``attempt()`` performs one ``order_send`` and returns:
        ("filled", result_dict)      -> accepted; ``result_dict`` is returned as-is
        ("retry",  comment, retcode) -> transiently closed; keep trying
        ("fatal",  comment, retcode) -> hard rejection; stop immediately

    ``should_abort()`` (optional) returns a reason string when the retry must be
    abandoned (curfew / kill-switch / news suppression arrived), else ``None``.

    Returns the final execution-result dict: a FILLED dict (as produced by
    ``attempt``) or a REJECTED dict carrying ``reason`` and ``retry_attempts``.
    """
    last_comment: str | None = None
    last_retcode = None
    attempts = 0

    while True:
        if should_abort is not None:
            reason = should_abort()
            if reason:
                return _rejected(
                    f"aborted before fill: {reason}", last_retcode, attempts, "aborted"
                )

        if now() >= deadline_epoch:
            return _rejected(
                last_comment or "market closed — retry window elapsed",
                last_retcode, attempts, "deadline",
            )

        kind, *rest = attempt()
        attempts += 1

        if kind == "filled":
            result = dict(rest[0])
            result["retry_attempts"] = attempts
            return result
        if kind == "fatal":
            return _rejected(rest[0], rest[1], attempts, "fatal")

        # kind == "retry": broker still reports the symbol closed
        last_comment, last_retcode = rest[0], rest[1]
        remaining = deadline_epoch - now()
        if remaining <= 0:
            return _rejected(last_comment, last_retcode, attempts, "deadline")
        sleep(min(interval, remaining))
