"""
Layer 1 — Gatekeeper

Receives TradingView webhook signals, validates them, runs the Finnhub
news filter, and forwards clean signals to Layer 2.

Environment variables (set in .env or system):
  FINNHUB_API_KEY       — required
  LAYER2_URL            — default http://127.0.0.1:8001/signal
  NEWS_WINDOW_MINUTES   — default 60
  NEWS_FAIL_OPEN        — default true
  TELEGRAM_BOT_TOKEN    — optional; enables suppression alerts
  TELEGRAM_CHAT_ID      — optional; required if BOT_TOKEN is set
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from layer1.news_filter import check_news_window

# ── Logging setup ─────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_log_file = LOG_DIR / f"layer1_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger("layer1")

# ── Config ────────────────────────────────────────────────────────────────
FINNHUB_KEY  = os.getenv("FINNHUB_API_KEY", "")        # no longer required — FF calendar used instead
LAYER2_URL   = os.getenv("LAYER2_URL",   "http://127.0.0.1:8001/signal")
NEWS_WINDOW  = int(os.getenv("NEWS_WINDOW_MINUTES", "60"))
FAIL_OPEN    = os.getenv("NEWS_FAIL_OPEN", "true").lower() == "true"

_TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# Dedup: (ticker, event_time_str) already alerted — prevents repeat alerts
# for the same blocking event.
_suppression_notified: set[tuple[str, str]] = set()

ALLOWED_PAIRS: frozenset[str] = frozenset({
    "EURUSD", "GBPUSD", "USDCHF", "USDCAD", "USDJPY",
    "NZDUSD", "XAUUSD", "XAGUSD", "NAS100",
})

# ── Telegram suppression alert ────────────────────────────────────────────

async def _alert_signal_blocked(signal: str, ticker: str, reason: str) -> None:
    """Fire-and-forget Telegram alert when a live signal is suppressed by news."""
    if not _TG_TOKEN or not _TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={
                "chat_id": _TG_CHAT_ID,
                "text": (
                    f"<b>TEE Alert</b>\n\n"
                    f"<b>Signal Blocked — {ticker} {signal}</b>\n\n"
                    f"{reason}\n\n"
                    f"<i>Trade not entered. Pair suspended until event passes.</i>"
                ),
                "parse_mode": "HTML",
            })
    except Exception as exc:
        logger.warning("Telegram suppression alert failed: %s", exc)


# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(title="TEE Layer 1 — Gatekeeper", version="1.0.0")

# ── Payload schema ────────────────────────────────────────────────────────
class SignalPayload(BaseModel):
    signal:       str    # "LONG" | "SHORT"
    ticker:       str
    timestamp_ms: int
    timeframe:    str
    entry:        float
    sl:           float
    tp:           float
    order_type:   str    # "LIMIT"
    rr_ratio:     float

    @field_validator("signal")
    @classmethod
    def _validate_signal(cls, v: str) -> str:
        v = v.upper()
        if v not in ("LONG", "SHORT"):
            raise ValueError(f"signal must be LONG or SHORT, got '{v}'")
        return v

    @field_validator("ticker")
    @classmethod
    def _validate_ticker(cls, v: str) -> str:
        v = v.upper()
        if v not in ALLOWED_PAIRS:
            raise ValueError(
                f"ticker '{v}' is not in the covered pair list — signal rejected"
            )
        return v

    @field_validator("entry", "sl", "tp")
    @classmethod
    def _validate_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price coordinates must be positive")
        return v

# ── Routes ────────────────────────────────────────────────────────────────
@app.post("/signal")
async def receive_signal(request: Request):
    """
    Main webhook endpoint — called by TradingView.

    Flow:
      1. Parse + validate payload schema and ticker.
      2. Run Finnhub news filter (±NEWS_WINDOW minutes around high-impact events).
      3. Forward clean signal to Layer 2, or suppress and log.
    """
    raw = await request.body()

    # 1. Parse & validate
    try:
        payload = SignalPayload.model_validate_json(raw)
    except Exception as exc:
        logger.warning("Rejected malformed payload: %s | body=%s", exc, raw[:300])
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info(
        "RECEIVED  %s %s | entry=%.5f  sl=%.5f  tp=%.5f  ts=%d",
        payload.signal, payload.ticker,
        payload.entry, payload.sl, payload.tp, payload.timestamp_ms,
    )

    # 2. News filter
    blocked, reason, event_time = await check_news_window(
        payload.ticker, FINNHUB_KEY, NEWS_WINDOW, FAIL_OPEN
    )

    if blocked:
        logger.warning(
            "SUPPRESSED %s %s — %s", payload.signal, payload.ticker, reason
        )
        # Send one Telegram alert per (ticker, event) pair — not on every signal
        if event_time:
            key = (payload.ticker, event_time)
            if key not in _suppression_notified:
                _suppression_notified.add(key)
                asyncio.create_task(
                    _alert_signal_blocked(payload.signal, payload.ticker, reason)
                )
        return JSONResponse(
            status_code=200,
            content={
                "status":  "suppressed",
                "ticker":  payload.ticker,
                "signal":  payload.signal,
                "reason":  reason,
            },
        )

    # 3. Forward to Layer 2
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                LAYER2_URL,
                content=raw,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("Layer 2 returned error %s: %s", exc.response.status_code, exc)
        raise HTTPException(status_code=502, detail="Layer 2 rejected the signal")
    except httpx.HTTPError as exc:
        logger.error("Layer 2 unreachable: %s", exc)
        raise HTTPException(status_code=503, detail="Layer 2 unavailable")

    logger.info(
        "FORWARDED %s %s → Layer 2 (%s)",
        payload.signal, payload.ticker, LAYER2_URL,
    )
    return JSONResponse(
        status_code=200,
        content={
            "status": "forwarded",
            "ticker": payload.ticker,
            "signal": payload.signal,
        },
    )


@app.get("/health")
async def health_check():
    """Liveness probe — called by monitoring and Layer 2 startup checks."""
    return {
        "status":         "ok",
        "layer":          1,
        "news_window_min": NEWS_WINDOW,
        "fail_open":      FAIL_OPEN,
        "utc_time":       datetime.now(timezone.utc).isoformat(),
    }
