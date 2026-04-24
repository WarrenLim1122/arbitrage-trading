"""
Finnhub economic calendar filter.

Fetches high-impact events and returns whether the current moment falls
within the suppression window for the currencies in the given ticker.
Results are cached for CACHE_TTL_MINUTES to avoid hammering the API.
"""

import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

# Finnhub country codes that carry each currency.
# EUR includes DE and FR because German/French data regularly moves EUR crosses.
CURRENCY_COUNTRIES: dict[str, list[str]] = {
    "EUR": ["EU", "DE", "FR"],   # Eurozone + Germany/France (most EUR-moving events)
    "GBP": ["GB"],
    "AUD": ["AU"],
    "CHF": ["CH"],
    "JPY": ["JP"],
    "USD": ["US"],
    # Only USD pairs are traded — other currencies kept for completeness
}

CACHE_TTL_MINUTES = 60
_cache: dict = {"events": None, "fetched_at": None}


async def _fetch_events(api_key: str) -> list[dict]:
    """Return cached Finnhub events, refreshing when the TTL expires."""
    now = datetime.now(timezone.utc)

    if _cache["events"] is not None and _cache["fetched_at"] is not None:
        age_minutes = (now - _cache["fetched_at"]).total_seconds() / 60
        if age_minutes < CACHE_TTL_MINUTES:
            return _cache["events"]

    date_from = (now - timedelta(hours=2)).strftime("%Y-%m-%d")
    date_to = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"token": api_key, "from": date_from, "to": date_to},
        )
        resp.raise_for_status()

    events = resp.json().get("economicCalendar", [])
    _cache["events"] = events
    _cache["fetched_at"] = now
    logger.info("Finnhub cache refreshed — %d events loaded", len(events))
    return events


async def check_news_window(
    ticker: str,
    api_key: str,
    window_minutes: int = 30,
    fail_open: bool = True,
) -> tuple[bool, str | None]:
    """
    Returns (is_blocked, reason_string).

    is_blocked=True  → caller must suppress the trade signal.
    fail_open=True   → treat Finnhub API failure as clear (pass signal through).
    fail_open=False  → treat Finnhub API failure as blocked (suppress signal).
    """
    base = ticker[:3].upper()
    quote = ticker[3:].upper()

    relevant_countries: set[str] = set(
        CURRENCY_COUNTRIES.get(base, []) + CURRENCY_COUNTRIES.get(quote, [])
    )

    try:
        events = await _fetch_events(api_key)
    except Exception as exc:
        logger.error("Finnhub fetch failed: %s", exc)
        if fail_open:
            logger.warning("Fail-open active — signal passed despite Finnhub error")
            return False, None
        return True, f"Finnhub unreachable ({exc}) — signal suppressed (fail-closed)"

    now = datetime.now(timezone.utc)
    window = timedelta(minutes=window_minutes)

    for event in events:
        if event.get("impact") != "high":
            continue
        if event.get("country") not in relevant_countries:
            continue

        try:
            event_dt = datetime.strptime(
                event["time"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue

        if abs(now - event_dt) <= window:
            reason = (
                f"[{event.get('country')}] {event.get('event')} "
                f"@ {event['time']} UTC  (within ±{window_minutes}min window)"
            )
            return True, reason

    return False, None
