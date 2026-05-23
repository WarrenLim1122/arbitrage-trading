"""Journal value-hygiene regressions (Issues 1 & 4).

Issue 1 — price fields written to Firestore must be rounded to each symbol's
natural digits so float artefacts such as 1.3465500000000001 never reach the
dashboard.

Issue 4 — MT5 deal.time / position.time are Unix timestamps expressed in the
*trade server's* timezone, not true UTC. The journal must convert them to real
UTC (so the dashboard renders the trade's actual exit time, not a value hours
off that looks like the journaling/recovery time).
"""
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _stub_mt5(monkeypatch):
    if "MetaTrader5" not in sys.modules:
        mt5 = types.ModuleType("MetaTrader5")
        for k, v in {
            "DEAL_ENTRY_IN": 0, "DEAL_ENTRY_OUT": 1,
            "DEAL_REASON_TP": 4, "DEAL_REASON_SL": 5, "DEAL_REASON_EXPERT": 7,
            "DEAL_REASON_MOBILE": 9, "DEAL_REASON_CLIENT": 10,
        }.items():
            setattr(mt5, k, v)
        sys.modules["MetaTrader5"] = mt5
    yield


def _jw():
    import importlib
    sys.modules.pop("layer3.journal.journaling_worker", None)
    return importlib.import_module("layer3.journal.journaling_worker")


# ── Issue 1: price digits + rounding ──────────────────────────────────────────

def test_price_digits_per_symbol():
    jw = _jw()
    assert jw._price_digits("USDJPY") == 3
    assert jw._price_digits("XAUUSD") == 2
    assert jw._price_digits("XAGUSD") == 4
    assert jw._price_digits("EURUSD") == 5
    assert jw._price_digits("GBPUSD") == 5


def test_round_price_kills_float_artifact():
    jw = _jw()
    # The exact artefact Warren saw in the journal TP column
    assert jw._round_price("GBPUSD", 1.3465500000000001) == 1.34655
    # Already-clean values pass straight through
    assert jw._round_price("XAUUSD", 4533.5) == 4533.5
    assert jw._round_price("NZDUSD", 0.584) == 0.584


def test_round_price_handles_none_and_bad_input():
    jw = _jw()
    assert jw._round_price("EURUSD", None) is None
    assert jw._round_price("EURUSD", "n/a") == "n/a"


# ── Issue 4: MT5 server → UTC offset ──────────────────────────────────────────

def test_server_offset_env_override(monkeypatch):
    jw = _jw()
    monkeypatch.setenv("MT5_SERVER_UTC_OFFSET_HOURS", "3")
    assert jw._mt5_server_utc_offset_hours(mt5_lock=None) == 3.0


def test_server_offset_bad_env_falls_back(monkeypatch):
    jw = _jw()
    monkeypatch.setenv("MT5_SERVER_UTC_OFFSET_HOURS", "not-a-number")
    # No fresh tick available in the test stub → safe 0.0 fallback (treat as UTC)
    assert jw._mt5_server_utc_offset_hours(mt5_lock=None) == 0.0


def test_server_offset_no_env_no_mt5_returns_zero(monkeypatch):
    jw = _jw()
    monkeypatch.delenv("MT5_SERVER_UTC_OFFSET_HOURS", raising=False)
    assert jw._mt5_server_utc_offset_hours(mt5_lock=None) == 0.0
