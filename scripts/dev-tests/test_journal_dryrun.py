"""
Dry-run test for the Layer 3 trade journaling pipeline.

Simulates a completed TP/SL trade without touching MT5, Firestore, or Firebase Storage.
Run from the project root:

    python scripts/test_journal_dryrun.py

What this tests:
  1. Chart rendering (matplotlib Agg backend)
  2. Firestore payload construction
  3. Dry-run log output (no writes)
  4. Document ID format
  5. WIN (TP) and LOSS (SL) scenarios for LONG and SHORT trades

On success you will see:
  - [DRY RUN] Firestore write skipped. + full JSON payload
  - Chart saved → generated_screenshots/<type>_<ticket>_outcome.png
  - [DRY RUN] Screenshot at: file://<path>
"""

import os
import sys
from pathlib import Path

# Allow running from project root without installing the package
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# ── Force dry-run mode ────────────────────────────────────────────────────────
os.environ["FIREBASE_JOURNAL_ENABLED"]   = "true"   # pipeline must be active
os.environ["FIREBASE_JOURNAL_DRY_RUN"]  = "true"   # no Firestore write
os.environ["SCREENSHOT_DRY_RUN"]        = "true"   # no Storage upload
os.environ["SCREENSHOT_ENABLED"]        = "true"
os.environ["SCREENSHOT_ONLY_FOR_TP_SL"] = "true"
os.environ["FIREBASE_JOURNAL_USER_ID"]  = "test-user-uid-12345"
os.environ["JOURNAL_ACCOUNT_TYPE"]      = "demo"
os.environ["JOURNAL_BROKER"]            = "MetaQuotes Demo"
os.environ["FIREBASE_BOT_NAME"]         = "HedgeHog Bot"
os.environ["FIREBASE_STRATEGY_NAME"]    = "Arbitrage Trading"
os.environ["SCREENSHOT_WIDTH"]          = "1600"
os.environ["SCREENSHOT_HEIGHT"]         = "900"
os.environ["SCREENSHOT_TIMEFRAME"]      = "M15"
os.environ["SCREENSHOT_LOOKBACK_BARS"]  = "120"

import logging
import numpy as np
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
)
logger = logging.getLogger("journal_dryrun")


def _make_fake_rates(
    base_price: float,
    n_bars: int = 150,
    bar_minutes: int = 15,
    seed: int = 42,
) -> np.ndarray:
    """Generate synthetic OHLCV data for offline chart testing."""
    rng = np.random.default_rng(seed)
    prices = [base_price]
    for _ in range(n_bars - 1):
        prices.append(prices[-1] * (1 + rng.normal(0, 0.0008)))

    base_ts = int(datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc).timestamp())
    times   = [base_ts + i * bar_minutes * 60 for i in range(n_bars)]

    rows = []
    for i, (t, p) in enumerate(zip(times, prices)):
        noise = rng.uniform(0.0001, 0.0008) * p
        o = p
        c = p + rng.normal(0, noise)
        h = max(o, c) + rng.uniform(0, noise)
        lo = min(o, c) - rng.uniform(0, noise)
        rows.append((t, o, h, lo, c, 100, 0, 0))

    dtype = np.dtype([
        ("time", np.int64), ("open", np.float64), ("high", np.float64),
        ("low", np.float64), ("close", np.float64),
        ("tick_volume", np.int64), ("spread", np.int32), ("real_volume", np.int64),
    ])
    arr = np.array(rows, dtype=dtype)
    return arr


def run_test(
    label: str,
    symbol: str,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    close_price: float,
    close_reason: str,
    ticket: int,
    open_ts: datetime,
    close_ts: datetime,
    gross_pnl: float,
    net_pnl: float,
) -> None:
    logger.info("=" * 60)
    logger.info("TEST: %s", label)
    logger.info("=" * 60)

    from layer3.journal.firebase_journal import (
        build_document_id, derive_market_type, FIREBASE_JOURNAL_COLLECTION,
        write_trade,
    )
    from layer3.journal.rr_chart_renderer import render_rr_chart

    account_type = os.environ["JOURNAL_ACCOUNT_TYPE"]
    mt5_account_id = "106497299"

    # Chart
    rates = _make_fake_rates(entry, n_bars=150)
    try:
        chart_path = render_rr_chart(
            rates=rates, symbol=symbol, direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp,
            close_price=close_price, close_time=close_ts, open_time=open_ts,
            outcome="WIN" if net_pnl >= 0 else "LOSS",
            net_pnl=net_pnl, volume=0.12, ticket=ticket,
            account_type=account_type, close_reason=close_reason,
            rr_ratio=round(abs(entry - tp) / abs(entry - sl), 2) if abs(entry - sl) > 0 else None,
        )
        logger.info("[DRY RUN] Screenshot at: file://%s", chart_path)
        screenshot_fields = {
            "outcomeScreenshotStatus":     "success",
            "outcomeScreenshotSource":     "python_script",
            "outcomeScreenshotCapturedAt": datetime.now(timezone.utc).isoformat(),
            "outcomeScreenshotReason":     close_reason,
            "outcomeScreenshotUrl":        f"file://{chart_path}",
            "rrChartUrl":                  f"file://{chart_path}",
            "rrChartSource":               "python_script",
            "rrChartCapturedAt":           datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error("Chart render failed: %s", exc)
        screenshot_fields = {"outcomeScreenshotStatus": "failed"}

    # Payload
    now_iso   = datetime.now(timezone.utc).isoformat()
    doc_id    = build_document_id(account_type, mt5_account_id, ticket)
    outcome   = "WIN" if net_pnl >= 0 else "LOSS"
    rr_ratio  = round(abs(entry - tp) / abs(entry - sl), 2) if abs(entry - sl) > 0 else None

    payload = {
        "id":           doc_id,
        "userId":       os.environ["FIREBASE_JOURNAL_USER_ID"],
        "source":       "bot",
        "botName":      os.environ["FIREBASE_BOT_NAME"],
        "strategyName": os.environ["FIREBASE_STRATEGY_NAME"],
        "accountType":  account_type,
        "broker":       os.environ["JOURNAL_BROKER"],
        "mt5AccountId": mt5_account_id,
        "ticket":       ticket,
        "magicNumber":  20250002,
        "marketType":   derive_market_type(symbol),
        "symbol":       symbol,
        "pair":         symbol,
        "direction":    direction,
        "position":     direction.capitalize(),
        "volume":       0.12,
        "entryPrice":   entry,
        "closePrice":   close_price,
        "stopLoss":     sl,
        "takeProfit":   tp,
        "openTime":     open_ts.isoformat(),
        "closeTime":    close_ts.isoformat(),
        "date":         close_ts.isoformat(),
        "closeReason":  close_reason,
        "grossPnl":     round(gross_pnl, 2),
        "commission":   -0.60,
        "swap":         0.0,
        "netPnl":       round(net_pnl, 2),
        "pnlAmount":    round(net_pnl, 2),
        "accountCurrency": "USD",
        "rrRatio":      rr_ratio,
        "outcome":      outcome,
        "tags":         ["bot", "arbitrage", "personal"],
        "notes":        "",
        **screenshot_fields,
        "createdAt":    now_iso,
        "updatedAt":    now_iso,
        "importedAt":   now_iso,
        "rawMt5Data":   {"positionTicket": ticket, "exitDealReason": 7},
    }

    ok = write_trade(payload)
    assert ok, "write_trade returned False in dry-run — check logs"
    logger.info("TEST PASSED: %s  doc_id=%s", label, doc_id)


if __name__ == "__main__":
    open_ts  = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    close_ts = datetime(2026, 5, 5, 14, 30, tzinfo=timezone.utc)

    # Test 1: LONG TP WIN — XAGUSD
    run_test(
        label="LONG TP WIN — XAGUSD",
        symbol="XAGUSD", direction="LONG",
        entry=32.800, sl=32.450, tp=33.150, close_price=33.150,
        close_reason="TP", ticket=846527101,
        open_ts=open_ts, close_ts=close_ts,
        gross_pnl=42.00, net_pnl=41.40,
    )

    # Test 2: SHORT SL LOSS — EURUSD
    run_test(
        label="SHORT SL LOSS — EURUSD",
        symbol="EURUSD", direction="SHORT",
        entry=1.08500, sl=1.08800, tp=1.08100, close_price=1.08800,
        close_reason="SL", ticket=846527102,
        open_ts=open_ts, close_ts=close_ts,
        gross_pnl=-36.00, net_pnl=-36.60,
    )

    # Test 3: SHORT TP WIN — XAUUSD
    run_test(
        label="SHORT TP WIN — XAUUSD",
        symbol="XAUUSD", direction="SHORT",
        entry=2345.00, sl=2355.00, tp=2330.00, close_price=2330.00,
        close_reason="TP", ticket=846527103,
        open_ts=open_ts, close_ts=close_ts,
        gross_pnl=180.00, net_pnl=178.80,
    )

    # Test 4: LONG SL LOSS — USDJPY
    run_test(
        label="LONG SL LOSS — USDJPY",
        symbol="USDJPY", direction="LONG",
        entry=153.200, sl=152.800, tp=153.800, close_price=152.800,
        close_reason="SL", ticket=846527104,
        open_ts=open_ts, close_ts=close_ts,
        gross_pnl=-31.20, net_pnl=-31.80,
    )

    logger.info("")
    logger.info("All dry-run tests passed.")
    logger.info("Screenshots saved under: generated_screenshots/")
    logger.info("")
    logger.info("Next steps to enable live journaling:")
    logger.info("  1. Set FIREBASE_PROJECT_ID, FIREBASE_SERVICE_ACCOUNT_PATH,")
    logger.info("     FIREBASE_JOURNAL_USER_ID, FIREBASE_STORAGE_BUCKET in .env")
    logger.info("  2. Set FIREBASE_JOURNAL_DRY_RUN=false")
    logger.info("  3. Set SCREENSHOT_DRY_RUN=false")
    logger.info("  4. Set FIREBASE_JOURNAL_ENABLED=true")
    logger.info("  5. Restart Layer 3 workers")
