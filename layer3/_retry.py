"""Pure market-order retry driver — no MetaTrader5 / zmq imports.

Gold (XAUUSD) closes for a short daily settlement/maintenance break. A MARKET
order fired during that window is rejected by the broker with retcode
``TRADE_RETCODE_MARKET_CLOSED`` ("Market closed"). Treating that single
rejection as terminal silently drops the signal — so the system never actually
enters the trade the user expects under a 24-hour trading window.

This driver re-attempts the same market order while the broker keeps reporting
the symbol transiently closed, and gives up only at the end of the signal's
15m *entry* bar (the staleness guard). Kept import-free so it is unit-testable
on the dev machine, where ``MetaTrader5`` is unavailable.
"""
from typing import Callable

BAR_SECONDS = 900  # the engine trades a single 15-minute timeframe


def signal_bar_deadline_epoch(timestamp_ms: int, bar_seconds: int = BAR_SECONDS) -> float:
    """Epoch-seconds deadline = end of the 15m bar the trade enters on.

    ``timestamp_ms`` is the signal bar's OPEN time. The bar closes (and the
    TradingView alert fires) at ``+bar_seconds``; the order is placed on the
    next bar, which ends at ``+2 * bar_seconds``.
    """
    return timestamp_ms / 1000.0 + 2 * bar_seconds


def _rejected(comment: str, retcode, attempts: int) -> dict:
    return {
        "status":         "REJECTED",
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
    """Re-attempt a market order until it fills, is aborted, or the bar ends.

    ``attempt()`` performs one ``order_send`` and returns:
        ("filled", result_dict)      -> accepted; ``result_dict`` is returned as-is
        ("retry",  comment, retcode) -> transiently closed; keep trying
        ("fatal",  comment, retcode) -> hard rejection; stop immediately

    ``should_abort()`` (optional) returns a reason string when the retry must be
    abandoned (curfew / kill-switch / news suppression arrived), else ``None``.

    Returns the final execution-result dict (``status`` FILLED or REJECTED),
    with a ``retry_attempts`` count for observability.
    """
    last_comment: str | None = None
    last_retcode = None
    attempts = 0

    while True:
        if should_abort is not None:
            reason = should_abort()
            if reason:
                return _rejected(f"aborted before fill: {reason}", last_retcode, attempts)

        if now() >= deadline_epoch:
            return _rejected(
                last_comment or "market closed — entry-bar deadline reached",
                last_retcode, attempts,
            )

        kind, *rest = attempt()
        attempts += 1

        if kind == "filled":
            result = dict(rest[0])
            result["retry_attempts"] = attempts
            return result
        if kind == "fatal":
            return _rejected(rest[0], rest[1], attempts)

        # kind == "retry": broker still reports the symbol closed
        last_comment, last_retcode = rest[0], rest[1]
        remaining = deadline_epoch - now()
        if remaining <= 0:
            return _rejected(last_comment, last_retcode, attempts)
        sleep(min(interval, remaining))
