"""
ForexFactory economic calendar client — no API key required.

Fetches from the public ForexFactory JSON endpoints (nfs.faireconomy.media).
The "date" field is an ISO 8601 string with timezone offset (e.g. "2026-04-30T19:00:00-04:00")
and is parsed directly to UTC.

Shared by Layer 1 (via asyncio.to_thread) and Layer 2 (directly in background thread).
"""

import logging
import threading
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL = 900  # seconds — refresh every 15 minutes

# Both weeks fetched so events near the Mon/Sun boundary are never missed.
_FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

_cache_lock = threading.Lock()
_cache: dict = {"events": None, "fetched_at": None}


def _parse_event_utc(date_str: str) -> datetime | None:
    """Parse a ForexFactory ISO 8601 date string into a UTC datetime.

    FF publishes dates as e.g. "2026-04-30T19:00:00-04:00".
    Returns None if the string is empty or unparseable.
    """
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).astimezone(timezone.utc)
    except ValueError:
        logger.debug("Unparseable FF date: %s", date_str)
        return None


def fetch_events_sync() -> list[dict]:
    """Return ForexFactory calendar events for this week + next week.

    Cached for 15 minutes. Thread-safe.

    Each event dict contains:
        currency (str)      — e.g. "USD", "EUR", "GBP"  (FF "country" field)
        title    (str)      — event name
        impact   (str)      — "High" | "Medium" | "Low" | "Holiday"
        time_utc (datetime) — event time in UTC, timezone-aware

    Events with no parseable fixed time are excluded.
    """
    now = datetime.now(timezone.utc)
    with _cache_lock:
        if (
            _cache["events"] is not None
            and _cache["fetched_at"] is not None
            and (now - _cache["fetched_at"]).total_seconds() < _CACHE_TTL
        ):
            return _cache["events"]

    events: list[dict] = []
    with httpx.Client(timeout=10.0) as client:
        for url in _FF_URLS:
            try:
                resp = client.get(url)
                resp.raise_for_status()
                for item in resp.json():
                    utc = _parse_event_utc(item.get("date", ""))
                    if utc is None:
                        continue
                    events.append({
                        "currency": item.get("country", "").upper(),  # FF calls it "country" but value is currency code
                        "title":    item.get("title",   ""),
                        "impact":   item.get("impact",  ""),
                        "time_utc": utc,
                    })
            except Exception as exc:
                logger.warning("FF calendar fetch failed (%s): %s", url, exc)

    if events:
        with _cache_lock:
            _cache["events"] = events
            _cache["fetched_at"] = now
        logger.info("FF calendar refreshed — %d timed events loaded", len(events))
    else:
        logger.warning("FF calendar: no events returned from either URL — using stale cache if available")
        with _cache_lock:
            if _cache["events"] is not None:
                return _cache["events"]

    return events
