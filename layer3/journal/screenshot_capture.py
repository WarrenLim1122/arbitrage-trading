"""
Orchestrate MT5 candle fetch → chart render → storage upload.

Called from journaling_worker.py inside a background thread.
Returns screenshot fields dict suitable for embedding in the Firestore payload.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SCREENSHOT_ENABLED        = os.getenv("SCREENSHOT_ENABLED",        "true").lower() == "true"
SCREENSHOT_ONLY_FOR_TP_SL = os.getenv("SCREENSHOT_ONLY_FOR_TP_SL", "true").lower() == "true"
SCREENSHOT_DRY_RUN        = os.getenv("SCREENSHOT_DRY_RUN",        "false").lower() == "true"
SCREENSHOT_TIMEFRAME      = os.getenv("SCREENSHOT_TIMEFRAME",      "M15")
SCREENSHOT_LOOKBACK_BARS  = int(os.getenv("SCREENSHOT_LOOKBACK_BARS", "120"))
SCREENSHOT_BUFFER_BARS    = 20   # bars after close time


_TF_MAP = {
    "M1":  1,  "M5":  5,  "M15": 15, "M30": 30,
    "H1":  60, "H4":  240, "D1": 1440,
}


def _fetch_rates(symbol: str, mt5_lock, open_time: datetime, close_time: datetime):
    """Fetch MT5 candle data with MT5 lock held only for the API call."""
    import MetaTrader5 as mt5

    tf_const_map = {
        "M1":  mt5.TIMEFRAME_M1,  "M5":  mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1":  mt5.TIMEFRAME_H1,  "H4":  mt5.TIMEFRAME_H4,
        "D1":  mt5.TIMEFRAME_D1,
    }
    tf       = tf_const_map.get(SCREENSHOT_TIMEFRAME, mt5.TIMEFRAME_M15)
    bar_mins = _TF_MAP.get(SCREENSHOT_TIMEFRAME, 15)

    from_dt = open_time  - timedelta(minutes=bar_mins * SCREENSHOT_LOOKBACK_BARS)
    to_dt   = close_time + timedelta(minutes=bar_mins * SCREENSHOT_BUFFER_BARS)

    with mt5_lock:
        rates = mt5.copy_rates_range(symbol, tf, from_dt, to_dt)

    return rates


def capture_outcome_screenshot(
    symbol: str,
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    close_price: float,
    close_time: datetime,
    open_time: datetime,
    outcome: str,
    net_pnl: float,
    volume: float,
    ticket: int,
    account_type: str,
    mt5_account_id: str,
    close_reason: str,
    mt5_lock,
    rr_ratio: Optional[float] = None,
) -> dict:
    """
    Full screenshot pipeline: fetch → render → upload.

    Always returns a dict of Firestore screenshot fields.
    Never raises — errors are caught and surfaced via outcomeScreenshotStatus.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    base = {
        "outcomeScreenshotSource":     "python_script",
        "outcomeScreenshotCapturedAt": now_iso,
        "outcomeScreenshotReason":     close_reason,
        "outcomeScreenshotStatus":     "failed",
        "outcomeScreenshotUrl":        None,
        "rrChartUrl":                  None,
        "rrChartSource":               "python_script",
        "rrChartCapturedAt":           now_iso,
    }

    if not SCREENSHOT_ENABLED:
        base["outcomeScreenshotStatus"] = "failed"
        return base

    if SCREENSHOT_ONLY_FOR_TP_SL and close_reason not in ("TP", "SL"):
        base["outcomeScreenshotStatus"] = "failed"
        return base

    try:
        # 1. Candle data (brief MT5 lock)
        rates = _fetch_rates(symbol, mt5_lock, open_time, close_time)
        if rates is None or len(rates) == 0:
            logger.warning("No candle data for %s — screenshot skipped", symbol)
            return base

        # 2. Render chart (no lock needed after rates fetched)
        from .rr_chart_renderer import render_rr_chart
        local_path = render_rr_chart(
            rates=rates, symbol=symbol, direction=direction,
            entry_price=entry_price, sl_price=sl_price, tp_price=tp_price,
            close_price=close_price, close_time=close_time, open_time=open_time,
            outcome=outcome, net_pnl=net_pnl, volume=volume,
            ticket=ticket, account_type=account_type, close_reason=close_reason,
            rr_ratio=rr_ratio,
        )

        if SCREENSHOT_DRY_RUN:
            logger.info("[DRY RUN] Screenshot at: %s", local_path)
            base["outcomeScreenshotStatus"] = "success"
            base["outcomeScreenshotUrl"]    = f"file://{local_path}"
            base["rrChartUrl"]              = f"file://{local_path}"
            return base

        # 3. Upload
        from .storage_uploader import upload_screenshot
        url = upload_screenshot(local_path, account_type, mt5_account_id, ticket)
        if url:
            base["outcomeScreenshotStatus"] = "success"
            base["outcomeScreenshotUrl"]    = url
            base["rrChartUrl"]              = url
        # else status stays "failed"

    except Exception as exc:
        logger.error("Screenshot pipeline error (ticket=%d): %s", ticket, exc)

    return base
