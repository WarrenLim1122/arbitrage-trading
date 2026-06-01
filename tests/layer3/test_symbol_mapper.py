"""Unit tests for the broker symbol mapper's pure resolution core.

No MetaTrader5 needed — everything operates on a supplied list of broker symbol
names, so these run anywhere.
"""
from layer3 import symbol_mapper as sm


def _map(canonical, available, overrides=None):
    mapping, missing, _ = sm.build_mapping(
        available, overrides=overrides or {}, symbols=[canonical]
    )
    return mapping.get(canonical), missing


def test_exact_match():
    assert sm.resolve_one("EURUSD", ["EURUSD", "GBPUSD"]) == ("EURUSD", "exact")


def test_dot_suffix():
    assert sm.resolve_one("EURUSD", ["EURUSD.a"]) == ("EURUSD.a", "suffix")


def test_micro_letter_suffix():
    assert sm.resolve_one("EURUSD", ["EURUSDm"]) == ("EURUSDm", "suffix")


def test_pro_and_raw_suffix():
    assert sm.resolve_one("USDSGD", ["USDSGD.pro"]) == ("USDSGD.pro", "suffix")
    assert sm.resolve_one("EURUSD", ["EURUSD.raw"]) == ("EURUSD.raw", "suffix")


def test_exact_beats_suffix():
    # Plain symbol present alongside a suffixed one → prefer the exact.
    broker, _ = sm.resolve_one("AUDUSD", ["AUDUSDT", "AUDUSD", "AUDUSD.a"])
    assert broker == "AUDUSD"


def test_shortest_suffix_wins():
    broker, _ = sm.resolve_one("EURUSD", ["EURUSD.proecn", "EURUSD.a"])
    assert broker == "EURUSD.a"


def test_cny_never_matches_cnh():
    # The classic trap: only the offshore CNH symbol exists; onshore CNY must
    # NOT silently map to it (different instrument).
    broker, missing = _map("USDCNY", ["USDCNH", "USDCNH.pro"])
    assert broker is None
    assert missing == ["USDCNY"]


def test_cnh_still_resolves_when_present():
    broker, _ = _map("USDCNH", ["USDCNH.pro", "USDCNY"])
    assert broker == "USDCNH.pro"


def test_missing_symbol_reported():
    broker, missing = _map("USDPKR", ["EURUSD", "GBPUSD.a"])
    assert broker is None
    assert missing == ["USDPKR"]


def test_override_takes_priority():
    # Manual override forces a specific broker symbol even if auto would differ.
    broker, _ = _map("EURUSD", ["EURUSD.a", "EURUSD.ecn"],
                     overrides={"EURUSD": "EURUSD.ecn"})
    assert broker == "EURUSD.ecn"


def test_override_ignored_when_absent_on_broker():
    # Override names a symbol the broker does not have → fall back to auto.
    broker, _ = _map("EURUSD", ["EURUSD.a"], overrides={"EURUSD": "EURUSD.ecn"})
    assert broker == "EURUSD.a"


def test_build_mapping_mixed():
    available = ["EURUSD.a", "GBPUSD.a", "USDSGD.pro", "USDJPY.a"]
    symbols = ["EURUSD", "GBPUSD", "USDSGD", "USDJPY", "USDPKR"]
    mapping, missing, details = sm.build_mapping(available, overrides={}, symbols=symbols)
    assert mapping == {
        "EURUSD": "EURUSD.a",
        "GBPUSD": "GBPUSD.a",
        "USDSGD": "USDSGD.pro",
        "USDJPY": "USDJPY.a",
    }
    assert missing == ["USDPKR"]
    assert len(details) == len(symbols)


def test_discover_populates_resolve_and_report():
    sm.discover(["EURUSD.a", "USDJPY.a"], login=999)
    assert sm.resolve("EURUSD") == "EURUSD.a"
    # Unmapped canonical degrades to identity (never crashes).
    assert sm.resolve("USDVND") == "USDVND"
    rep = sm.last_report()
    assert "EURUSD" in rep["found"]


def test_registry_has_all_33_canonicals():
    syms = sm.supported_symbols()
    assert len(syms) == 33
    assert "XAUUSD" in syms and "XAGUSD" in syms and "AUDUSD" in syms
