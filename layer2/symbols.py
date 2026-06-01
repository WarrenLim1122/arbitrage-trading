"""
Canonical symbol registry — THE single source of truth for the whole stack.

`config/symbols.json` lists every tradeable instrument by its **TradingView
name** (the permanent canonical standard). Layer 1 (gatekeeper), Layer 2 (risk)
and Layer 3 (execution) all derive their symbol lists from here — so adding a
pair is a one-line edit to that JSON, never a code change in three places.

  TradingView name  ─►  canonical (this module)  ─►  broker MT5 name (Layer 3)

Only `layer3/symbol_mapper.py` ever translates canonical → broker name. Layers 1
and 2 never see a broker suffix.

This module lives under `layer2/` only because the OS forbids creating a new
top-level package here; it is logically layer-agnostic and imports nothing but
the stdlib, so Layer 1 and Layer 3 import it without side effects.

Per-symbol metadata:
  currencies — ISO codes that drive the Layer 1 news filter (USD-only for metals)
  tier       — "major" | "asian" | "other" | "exotic" | "metal" (informational)

To add a future pair (e.g. USDMXN): add one line to config/symbols.json and
restart. Layer 3 auto-discovers the broker symbol, validates it, and caches it.
"""
from __future__ import annotations

import json
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "symbols.json"


def _load() -> dict[str, dict]:
    with REGISTRY_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, dict] = {}
    for sym, meta in raw.items():
        out[sym.upper()] = {
            "currencies": [c.upper() for c in meta.get("currencies", [])],
            "tier": meta.get("tier", "unknown"),
        }
    return out


_REGISTRY: dict[str, dict] = _load()

# Canonical TradingView names, in registry order.
SUPPORTED_SYMBOLS: list[str] = list(_REGISTRY.keys())

# Layer 1 gate — the set of tickers accepted from TradingView.
ALLOWED_PAIRS: frozenset[str] = frozenset(SUPPORTED_SYMBOLS)

# Layer 1 news filter — canonical ticker → ISO currency codes it is exposed to.
TICKER_CURRENCIES: dict[str, frozenset[str]] = {
    sym: frozenset(meta["currencies"]) for sym, meta in _REGISTRY.items()
}


def is_supported(symbol: str) -> bool:
    return symbol.upper() in _REGISTRY


def currencies_for(symbol: str) -> frozenset[str]:
    """ISO currency codes a ticker is exposed to (for the news filter)."""
    return TICKER_CURRENCIES.get(symbol.upper(), frozenset())


def tier_of(symbol: str) -> str:
    meta = _REGISTRY.get(symbol.upper())
    return meta["tier"] if meta else "unknown"


def registry() -> dict[str, dict]:
    """Full canonical registry (copy)."""
    return {k: dict(v) for k, v in _REGISTRY.items()}
