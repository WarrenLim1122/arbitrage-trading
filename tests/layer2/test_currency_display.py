"""Issue 7 — per-account currency display (SGD personal / USD prop).

Lot-sizing and risk MATH stay USD; only the *display* of personal-account figures
becomes SGD, with personal Risk/Reward shown as USD plus an SGD equivalent.
"""
from layer2.state import _money, _ccy_prefix
from layer2.logic_core import _split_pers_amount, _pers_money_dual


# ── state._money / _ccy_prefix ────────────────────────────────────────────────

def test_ccy_prefix():
    assert _ccy_prefix("USD") == "$"
    assert _ccy_prefix("usd") == "$"
    assert _ccy_prefix("SGD") == "SGD "
    assert _ccy_prefix("") == "$"        # default USD
    assert _ccy_prefix(None) == "$"


def test_money_usd():
    assert _money(1234.56, "USD") == "$1,234.56"
    assert _money(670, "USD", signed=True) == "$+670.00"
    assert _money(-774.5, "USD", signed=True) == "$-774.50"


def test_money_sgd_label_format():
    # Warren chose the "SGD 1,234.56" label (ISO code + space prefix)
    assert _money(1234.56, "SGD") == "SGD 1,234.56"
    assert _money(-774.5, "SGD", signed=True) == "SGD -774.50"
    assert _money(1514.76, "SGD", signed=True) == "SGD +1,514.76"


# ── logic_core._split_pers_amount ─────────────────────────────────────────────
# Geometry computes USD-quote pairs (ticker endswith USD) in USD via contract size,
# and USD-base pairs in the account currency via tick_value. The split recovers both.

def test_split_usd_quote_pair():
    # EURUSD: geometry value is USD → (usd, sgd) = (670, 670*rate)
    usd, acct = _split_pers_amount("EURUSD", 670.0, 1.35)
    assert usd == 670.0
    assert acct == 904.5


def test_split_usd_base_pair():
    # USDJPY: geometry value is account currency (SGD) → (value/rate, value)
    usd, acct = _split_pers_amount("USDJPY", 904.5, 1.35)
    assert usd == 670.0
    assert acct == 904.5


def test_split_rate_unknown_passes_through():
    usd, acct = _split_pers_amount("EURUSD", 500.0, 0.0)   # rate 0 → treat as 1.0
    assert usd == 500.0 and acct == 500.0


# ── logic_core._pers_money_dual ───────────────────────────────────────────────

def test_dual_usd_account_is_plain_usd():
    # USD personal account → no SGD parenthetical, behaviour unchanged
    assert _pers_money_dual("EURUSD", 670.0, "USD", 1.0) == "$670.00"


def test_dual_sgd_account_shows_both():
    out = _pers_money_dual("EURUSD", 670.0, "SGD", 1.35)
    assert out == "$670.00 (≈ SGD 904.50)"


def test_dual_sgd_usd_base_pair():
    out = _pers_money_dual("USDJPY", 904.5, "SGD", 1.35)
    assert out == "$670.00 (≈ SGD 904.50)"
