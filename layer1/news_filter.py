"""
News window filter — powered by ForexFactory calendar (no API key required).

ForexFactory is the industry-standard reference used by prop firms and traders.
Impact levels ("High", "Medium", "Low") match the ForexFactory red/orange/yellow icons.
Currency codes ("USD", "EUR", "GBP") map directly to pair base/quote currencies.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from layer1.ff_calendar import fetch_events_sync
from layer2.symbols import TICKER_CURRENCIES as _TICKER_CURRENCIES

logger = logging.getLogger(__name__)

# _TICKER_CURRENCIES maps each pair to the currencies that can trigger a news
# block. It is derived from the canonical registry (config/symbols.json) so a
# new pair's news-currency exposure comes for free. ForexFactory tags events
# with the currency code directly (e.g. "EUR" for ECB, "USD" for Fed).


async def check_news_window(
    ticker: str,
    api_key: str,               # kept for interface compatibility — no longer used
    window_minutes: int = 60,
    fail_open: bool = True,
) -> tuple[bool, str | None, str | None]:
    """
    Returns (is_blocked, reason_string, event_time_key).

    is_blocked=True   → caller must suppress the trade signal.
    event_time_key    → ISO UTC string of the blocking event (for dedup).
    fail_open=True    → treat fetch failure as clear (pass signal through).
    fail_open=False   → treat fetch failure as blocked (suppress signal).
    """
    currencies = _TICKER_CURRENCIES.get(ticker.upper(), frozenset())
    if not currencies:
        return False, None, None

    try:
        events = await asyncio.to_thread(fetch_events_sync)
    except Exception as exc:
        logger.error("FF calendar fetch failed: %s", exc)
        if fail_open:
            logger.warning("Fail-open active — signal passed despite FF calendar error")
            return False, None, None
        return True, f"FF calendar unreachable ({exc}) — signal suppressed (fail-closed)", None

    now    = datetime.now(timezone.utc)
    window = timedelta(minutes=window_minutes)

    for event in events:
        if event.get("impact") != "High":
            continue
        if event.get("currency") not in currencies:
            continue

        event_utc = event.get("time_utc")
        if event_utc is None or abs(now - event_utc) > window:
            continue

        mins_away = int((event_utc - now).total_seconds() / 60)
        direction = f"in {mins_away} min" if mins_away >= 0 else f"{abs(mins_away)} min ago"
        reason = (
            f"[{event['currency']}] {event['title']} "
            f"@ {event_utc.strftime('%Y-%m-%d %H:%M')} UTC  ({direction}, ±{window_minutes}min window)"
        )
        return True, reason, event_utc.isoformat()

    return False, None, None
