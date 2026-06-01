"""
Universal symbol mapper — the ONLY place in the stack that knows broker naming.

    canonical (TradingView name)  ─►  broker MT5 symbol  ─►  execution

Layers 1 and 2 deal exclusively in canonical names. Layer 3 calls `resolve()`
to translate a canonical ticker into whatever this broker actually calls it
(`EURUSD`, `EURUSD.a`, `EURUSDm`, `EURUSD.pro`, `EURUSD.raw`, …). The broker's
convention is **discovered**, never hardcoded.

Resolution order (per canonical ticker), best match wins:
  0. manual override   — config/symbol_map.json {canonical: broker}, if the
                         named broker symbol actually exists. Highest priority;
                         the human escape hatch for anything auto-discovery
                         gets wrong.
  1. exact             — broker symbol normalizes to the canonical exactly.
  2. suffix            — broker symbol == canonical + a short suffix
                         (".a", "m", ".pro", ".raw", "-ECN", …). The suffix is
                         not enumerated; any short tail is accepted.
  3. separated-prefix  — separators inside the name (e.g. "EUR/USD.a").
  4. guarded fuzzy     — difflib last resort, re-validated through steps 1-3 so
                         it can NEVER cross currencies.

CRITICAL GUARD — every tier requires the broker symbol to *start with the full
canonical* (all six currency letters). That is what stops `USDCNY` silently
mapping to `USDCNH.pro` — two different instruments (onshore NDF vs offshore
deliverable). A symbol that does not begin with the exact canonical is never a
candidate; a miss is reported loudly, never papered over with a wrong match.

Discovered mappings are cached per broker at
`config/symbol_cache_<login>.json` and rebuilt automatically on every startup
(a single `symbols_get()` scan), so a broker renaming or adding symbols is
picked up without code changes.
"""
from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("layer3.symbol_mapper")

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_REGISTRY_PATH = _CONFIG_DIR / "symbols.json"
_OVERRIDES_PATH = _CONFIG_DIR / "symbol_map.json"   # manual {canonical: broker}

# Max length of an accepted suffix tail (normalized). Covers ".pro"/"-ECN"/"m"
# etc. without matching a second currency code glued on (which would be ≥3 and
# is additionally blocked by the start-with-full-canonical guard).
_MAX_SUFFIX = 4

# Runtime mapping populated by discover(); resolve() reads it on the hot path.
_mapping: dict[str, str] = {}
_last_report: dict | None = None


# ── Canonical list (single source of truth = config/symbols.json) ──────────

def supported_symbols() -> list[str]:
    """Canonical TradingView names from the shared registry."""
    with _REGISTRY_PATH.open(encoding="utf-8") as f:
        return [s.upper() for s in json.load(f).keys()]


def _load_overrides() -> dict[str, str]:
    if not _OVERRIDES_PATH.exists():
        return {}
    try:
        with _OVERRIDES_PATH.open(encoding="utf-8") as f:
            raw = json.load(f)
        return {k.upper(): v for k, v in raw.items() if isinstance(v, str)}
    except Exception as exc:
        logger.warning("symbol_map.json (overrides) unreadable: %s", exc)
        return {}


# ── Pure matching core (no MT5 — unit-tested off-VPS) ──────────────────────

def _norm(s: str) -> str:
    """Uppercase, alphanumerics only (drops '.', '-', '/', '_', spaces)."""
    return "".join(ch for ch in s.upper() if ch.isalnum())


def _match(canonical: str, broker: str) -> tuple[int, int] | None:
    """Score one broker symbol against a canonical. Lower = better; None = no
    candidate. The first element is the tier (0 exact / 1 suffix / 2 sep-prefix),
    the second is the suffix length (shorter wins on ties)."""
    c = canonical.upper()
    up = broker.upper()
    norm = _norm(broker)

    if norm == c:
        return (0, 0)
    # Require the *raw* name to start with the full canonical: guarantees both
    # currency codes match before any tail is considered (USDCNY ≠ USDCNH...).
    if up.startswith(c):
        rem = _norm(up[len(c):])
        if len(rem) <= _MAX_SUFFIX:
            return (1, len(rem))
    elif norm.startswith(c):
        rem = norm[len(c):]
        if len(rem) <= _MAX_SUFFIX:
            return (2, len(rem))
    return None


_KIND = {0: "exact", 1: "suffix", 2: "sep-prefix"}


def resolve_one(canonical: str, available: list[str]) -> tuple[str | None, str]:
    """Best broker symbol for one canonical, scanning `available`. Returns
    (broker_symbol_or_None, match_kind)."""
    best: tuple[tuple[int, int], str] | None = None
    for name in available:
        score = _match(canonical, name)
        if score is None:
            continue
        if best is None or score < best[0] or (score == best[0] and len(name) < len(best[1])):
            best = (score, name)
    if best is not None:
        return best[1], _KIND[best[0][0]]
    return None, "missing"


def _fuzzy(canonical: str, available: list[str]) -> str | None:
    """Last-resort fuzzy match — re-validated through `_match`, so it can only
    ever pick a symbol that still starts with the full canonical. In practice a
    safe no-op tie-breaker; present so an odd separator scheme still resolves
    without ever crossing currencies."""
    c = canonical.upper()
    norm_to_name: dict[str, str] = {}
    for n in available:
        norm_to_name.setdefault(_norm(n), n)
    for cand in difflib.get_close_matches(c, list(norm_to_name), n=3, cutoff=0.9):
        name = norm_to_name[cand]
        if _match(c, name) is not None:
            return name
    return None


def build_mapping(
    available: list[str],
    overrides: dict[str, str] | None = None,
    symbols: list[str] | None = None,
) -> tuple[dict[str, str], list[str], list[tuple[str, str | None, str]]]:
    """Resolve every canonical against `available`.

    Returns (mapping, missing, details):
      mapping  — {canonical: broker_symbol} for everything that resolved
      missing  — canonicals with no broker symbol on this account
      details  — [(canonical, broker_or_None, kind)] for the full report table
    """
    overrides = _load_overrides() if overrides is None else overrides
    symbols = supported_symbols() if symbols is None else symbols
    avail_set = set(available)

    mapping: dict[str, str] = {}
    missing: list[str] = []
    details: list[tuple[str, str | None, str]] = []

    for c in symbols:
        ov = overrides.get(c)
        if ov and ov in avail_set:
            mapping[c] = ov
            details.append((c, ov, "override"))
            continue
        if ov and ov not in avail_set:
            logger.warning("override %s -> %s ignored (not on broker); auto-resolving", c, ov)

        name, kind = resolve_one(c, available)
        if name is None:
            name = _fuzzy(c, available)
            kind = "fuzzy" if name else "missing"
        if name is None:
            missing.append(c)
            details.append((c, None, "missing"))
        else:
            mapping[c] = name
            details.append((c, name, kind))

    return mapping, missing, details


# ── Cache I/O ──────────────────────────────────────────────────────────────

def _cache_path(login) -> Path:
    return _CONFIG_DIR / f"symbol_cache_{login}.json"


def _write_cache(login, mapping: dict[str, str]) -> None:
    try:
        payload = {
            "login": login,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "mapping": mapping,
        }
        with _cache_path(login).open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as exc:
        logger.warning("symbol cache write failed: %s", exc)


# ── Orchestration ──────────────────────────────────────────────────────────

def discover(available: list[str], login) -> dict:
    """Build + cache the mapping from a list of broker symbol names.

    `available` = [symbol.name for symbol in mt5.symbols_get()]. Pure w.r.t.
    MT5 (the caller fetches names), so it is fully unit-testable. Populates the
    runtime mapping used by resolve(); the caller then symbol_select()s every
    resolved name into Market Watch.
    """
    global _mapping, _last_report
    symbols = supported_symbols()
    mapping, missing, details = build_mapping(available, symbols=symbols)
    _mapping = mapping
    _last_report = {
        "supported": symbols,
        "mapping": mapping,
        "found": list(mapping.keys()),
        "missing": missing,
        "details": details,
    }
    _write_cache(login, mapping)
    return _last_report


def resolve(canonical: str) -> str:
    """Translate a canonical ticker to this broker's MT5 symbol. Falls back to
    the canonical itself if discovery has not run (degrades to identity, the
    pre-mapper behaviour) so a resolve() before discover() never crashes."""
    return _mapping.get(canonical.upper(), canonical)


def last_report() -> dict | None:
    """Most recent discover() report (for /checksymbols)."""
    return _last_report


def format_report(report: dict) -> str:
    """Plain-text [OK]/[ERROR] validation table (logged at startup)."""
    lines: list[str] = []
    for canonical, broker, kind in report["details"]:
        if broker:
            tag = "" if kind in ("exact", "override") else f"  ({kind})"
            lines.append(f"[OK]    {canonical} -> {broker}{tag}")
        else:
            lines.append(f"[ERROR] {canonical} not found on broker")
    n_sup = len(report["supported"])
    n_found = len(report["found"])
    lines.append(f"SUPPORTED: {n_sup}  FOUND: {n_found}  MISSING: {n_sup - n_found}")
    return "\n".join(lines)
