"""Regression test for the pending-deals screenshot retry.

Bug context: a force-closed trade's Phase 1 immediate screenshot may fail
(no candle data yet, transient upload error, or the close_reason was
"MARKET" before close_reason_override was stamped). The failed result used
to be cached in pos_snapshot["_screenshot_fields"] and reused on every
pending-queue retry — so even after deal history finally arrived hours later
and the trade was journaled to Firestore, the dashboard stayed stuck on
"No Image". This test pins the new behaviour: the recovery path retries the
screenshot capture using the real deal data.
"""
import os
import sys
import types
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# Make journaling_worker importable without MetaTrader5 + Google Cloud installed
# on the test machine. We only need the pure-Python recovery branch.
@pytest.fixture(autouse=True)
def _stub_external_modules(monkeypatch):
    if "MetaTrader5" not in sys.modules:
        mt5 = types.ModuleType("MetaTrader5")
        mt5.DEAL_ENTRY_IN  = 0
        mt5.DEAL_ENTRY_OUT = 1
        mt5.DEAL_REASON_TP     = 4
        mt5.DEAL_REASON_SL     = 5
        mt5.DEAL_REASON_EXPERT = 7
        mt5.DEAL_REASON_MOBILE = 9
        mt5.DEAL_REASON_CLIENT = 10
        sys.modules["MetaTrader5"] = mt5
    yield


def _import_worker():
    # Force JOURNAL_ENABLED so handle_closed_position runs the real path
    os.environ["FIREBASE_JOURNAL_ENABLED"] = "true"
    # importlib.import_module (not `from pkg import sub`) forces re-execution after
    # the pop — `from pkg import sub` returns the stale parent attribute if another
    # test imported the module first, leaving JOURNAL_ENABLED at its old value.
    import importlib
    sys.modules.pop("layer3.journal.journaling_worker", None)
    return importlib.import_module("layer3.journal.journaling_worker")


def _fake_deal(ticket, position_id, *, entry, price, time_ts,
               profit=0.0, commission=0.0, swap=0.0, volume=0.1, reason=7):
    d = types.SimpleNamespace()
    d.ticket      = ticket
    d.position_id = position_id
    d.entry       = entry
    d.price       = price
    d.time        = time_ts
    d.profit      = profit
    d.commission  = commission
    d.swap        = swap
    d.volume      = volume
    d.reason      = reason
    return d


def test_recovered_trade_retries_failed_screenshot(monkeypatch):
    """When deal history arrives and the cached screenshot is 'failed', the
    pipeline retries capture instead of writing None to Firestore."""
    journaling_worker = _import_worker()

    position_ticket = 8710836576
    open_ts  = datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
    close_ts = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)

    pos_snapshot = {
        "ticket":     position_ticket,
        "symbol":     "NZDUSD",
        "type":       1,        # SHORT
        "volume":     6.8,
        "price_open": 0.58216,
        "sl":         0.584,
        "tp":         0.58151,
        "magic":      20250001,
        "open_time":  open_ts,
        "close_reason_override": "KILL_3",
        # Cached Phase 1 attempt failed (the exact state pending-queue would replay)
        "_screenshot_fields": {
            "outcomeScreenshotStatus": "failed",
            "outcomeScreenshotSource": "python_script",
            "outcomeScreenshotUrl":    None,
            "rrChartUrl":              None,
        },
    }

    entry_deal = _fake_deal(1, position_ticket, entry=0,
                            price=0.58216, time_ts=int(open_ts.timestamp()),
                            commission=-2.5, volume=6.8, reason=7)
    exit_deal  = _fake_deal(2, position_ticket, entry=1,
                            price=0.584, time_ts=int(close_ts.timestamp()),
                            profit=-1156.0, commission=-2.5, volume=6.8, reason=7)

    # ── Capture what the recovery path passes to the screenshot pipeline ──
    captured = {}
    def fake_capture(**kwargs):
        captured.update(kwargs)
        return {
            "outcomeScreenshotStatus": "success",
            "outcomeScreenshotUrl":    "https://storage.googleapis.com/test/outcome.png?t=1",
            "outcomeScreenshotSource": "python_script",
            "rrChartUrl":              "https://storage.googleapis.com/test/outcome.png?t=1",
        }

    fake_capture_mod = types.ModuleType("layer3.journal.screenshot_capture")
    fake_capture_mod.capture_outcome_screenshot = fake_capture
    monkeypatch.setitem(sys.modules, "layer3.journal.screenshot_capture", fake_capture_mod)

    fake_firebase = types.ModuleType("layer3.journal.firebase_journal")
    fake_firebase.build_document_id    = lambda *a, **kw: "doc-1"
    fake_firebase.derive_market_type   = lambda symbol: "forex"
    written_payloads = []
    fake_firebase.write_trade          = lambda payload: written_payloads.append(payload) or True
    monkeypatch.setitem(sys.modules, "layer3.journal.firebase_journal", fake_firebase)

    fake_retry_queue = types.ModuleType("layer3.journal.retry_queue")
    fake_retry_queue.enqueue = lambda payload: None
    monkeypatch.setitem(sys.modules, "layer3.journal.retry_queue", fake_retry_queue)

    monkeypatch.setattr(
        journaling_worker, "_get_deals",
        lambda mt5_lock, position_ticket, open_time: ([entry_deal], [exit_deal]),
    )

    result = journaling_worker.handle_closed_position(
        mt5_lock=None,
        mt5_account_id="12345",
        worker_name="prop",
        position_ticket=position_ticket,
        pos_snapshot=pos_snapshot,
        skip_retry=True,
    )

    assert result is not None, "Recovery should journal successfully"
    assert captured, "Screenshot retry should have been invoked"
    assert captured["close_reason"] == "KILL_3"
    assert captured["close_price"] == 0.584         # real exit deal price, not snapshot estimate
    assert captured["net_pnl"] == -1161.00           # gross + commissions, rounded

    assert len(written_payloads) == 1
    written = written_payloads[0]
    assert written["outcomeScreenshotStatus"] == "success"
    assert written["outcomeScreenshotUrl"]    == "https://storage.googleapis.com/test/outcome.png?t=1"


def test_recovered_trade_keeps_successful_cached_screenshot(monkeypatch):
    """If Phase 1 succeeded, recovery must NOT re-upload — that would burn
    bandwidth and overwrite a known-good URL with a fresh timestamp."""
    journaling_worker = _import_worker()

    position_ticket = 999
    open_ts  = datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc)
    close_ts = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)

    pos_snapshot = {
        "ticket": position_ticket, "symbol": "XAUUSD",
        "type": 1, "volume": 0.5, "price_open": 4463.02,
        "sl": 4508.91, "tp": 4453.71, "magic": 20250001,
        "open_time": open_ts,
        "close_reason_override": "KILL_3",
        "_screenshot_fields": {
            "outcomeScreenshotStatus": "success",
            "outcomeScreenshotUrl":    "https://storage.googleapis.com/test/already-good.png?t=99",
            "outcomeScreenshotSource": "python_script",
            "rrChartUrl":              "https://storage.googleapis.com/test/already-good.png?t=99",
        },
    }

    entry_deal = _fake_deal(1, position_ticket, entry=0, price=4463.02,
                            time_ts=int(open_ts.timestamp()), volume=0.5)
    exit_deal  = _fake_deal(2, position_ticket, entry=1, price=4508.91,
                            time_ts=int(close_ts.timestamp()),
                            profit=-774.5, volume=0.5)

    fake_capture_mod = types.ModuleType("layer3.journal.screenshot_capture")
    fake_capture_mod.capture_outcome_screenshot = lambda **kw: (_ for _ in ()).throw(
        AssertionError("Successful cached screenshot should NOT be re-captured")
    )
    monkeypatch.setitem(sys.modules, "layer3.journal.screenshot_capture", fake_capture_mod)

    fake_firebase = types.ModuleType("layer3.journal.firebase_journal")
    fake_firebase.build_document_id    = lambda *a, **kw: "doc-2"
    fake_firebase.derive_market_type   = lambda symbol: "metal"
    written = []
    fake_firebase.write_trade          = lambda payload: written.append(payload) or True
    monkeypatch.setitem(sys.modules, "layer3.journal.firebase_journal", fake_firebase)

    fake_retry_queue = types.ModuleType("layer3.journal.retry_queue")
    fake_retry_queue.enqueue = lambda payload: None
    monkeypatch.setitem(sys.modules, "layer3.journal.retry_queue", fake_retry_queue)

    monkeypatch.setattr(
        journaling_worker, "_get_deals",
        lambda mt5_lock, position_ticket, open_time: ([entry_deal], [exit_deal]),
    )

    result = journaling_worker.handle_closed_position(
        mt5_lock=None, mt5_account_id="12345", worker_name="prop",
        position_ticket=position_ticket, pos_snapshot=pos_snapshot, skip_retry=True,
    )

    assert result is not None
    assert written[0]["outcomeScreenshotUrl"] == "https://storage.googleapis.com/test/already-good.png?t=99"
