"""Worker order_check verdict mapping (Issue 2).

Layer 2 calls _build_order_check_reply on BOTH legs before placing either order.
Its verdict ("ok" / "reject" / "transient") decides whether the trade is placed
at all, so the retcode → verdict mapping must be exact:

  * enough money            → ok
  * Not enough money        → reject  (this is the orphan-causing case)
  * invalid stops/volume    → reject
  * market closed / requote → transient (let the normal retry/limit path handle it)
"""
import os
import sys
import types

import pytest

_CONSTS = {
    "TRADE_ACTION_DEAL": 1, "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1,
    "ORDER_TIME_GTC": 0,
    "TRADE_RETCODE_DONE": 10009, "TRADE_RETCODE_NO_MONEY": 10019,
    "TRADE_RETCODE_MARKET_CLOSED": 10018, "TRADE_RETCODE_REQUOTE": 10004,
    "TRADE_RETCODE_PRICE_OFF": 10021, "TRADE_RETCODE_PRICE_CHANGED": 10020,
    "TRADE_RETCODE_INVALID_STOPS": 10016,
    "DEAL_ENTRY_IN": 0, "DEAL_ENTRY_OUT": 1,
    "TRADE_ACTION_REMOVE": 2, "TRADE_ACTION_PENDING": 5,
    "ORDER_TYPE_BUY_LIMIT": 2, "ORDER_TYPE_SELL_LIMIT": 3,
    "DEAL_REASON_TP": 4, "DEAL_REASON_SL": 5, "DEAL_REASON_EXPERT": 7,
    "DEAL_REASON_MOBILE": 9, "DEAL_REASON_CLIENT": 10,
    "ORDER_FILLING_IOC": 1, "ORDER_FILLING_FOK": 0, "ORDER_FILLING_RETURN": 2,
}


@pytest.fixture
def wc(monkeypatch):
    for k, v in {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
                 "MT5_LOGIN": "1", "MT5_PASSWORD": "p", "MT5_SERVER": "s"}.items():
        monkeypatch.setenv(k, v)
    mt5 = types.ModuleType("MetaTrader5")
    for k, v in _CONSTS.items():
        setattr(mt5, k, v)
    mt5.last_error = lambda: (0, "ok")
    mt5.symbol_info_tick = lambda sym: types.SimpleNamespace(ask=1.1, bid=1.0, time=0)
    mt5.terminal_info = lambda: types.SimpleNamespace(trade_allowed=True)
    mt5.order_check = None  # set per-test
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)

    sys.modules.pop("layer3._worker_core", None)
    import importlib
    module = importlib.import_module("layer3._worker_core")
    # Neutralise environment-specific helpers
    monkeypatch.setattr(module, "_ensure_connected", lambda: None)
    monkeypatch.setattr(module, "_resolve_symbol", lambda c: c)
    monkeypatch.setattr(module, "_get_filling_mode", lambda r: 1)
    return module, mt5


def _check_result(**kw):
    base = dict(retcode=10009, margin=500.0, margin_free=9000.0,
                balance=10000.0, equity=10000.0, comment="Done")
    base.update(kw)
    return types.SimpleNamespace(**base)


def _order():
    return {"ticker": "EURUSD", "signal": "LONG", "lots": 0.1, "sl": 0.99, "tp": 1.2}


def test_verdict_ok_when_enough_money(wc):
    module, mt5 = wc
    mt5.order_check = lambda req: _check_result(retcode=10009, margin_free=9000.0)
    out = module._build_order_check_reply(_order())
    assert out["verdict"] == "ok"


def test_verdict_ok_retcode_zero(wc):
    module, mt5 = wc
    mt5.order_check = lambda req: _check_result(retcode=0, margin_free=9000.0)
    assert module._build_order_check_reply(_order())["verdict"] == "ok"


def test_verdict_reject_no_money(wc):
    module, mt5 = wc
    mt5.order_check = lambda req: _check_result(
        retcode=10019, margin=12000.0, margin_free=-2000.0, comment="Not enough money")
    out = module._build_order_check_reply(_order())
    assert out["verdict"] == "reject"
    assert out["retcode"] == 10019


def test_verdict_reject_negative_free_margin_even_if_code_ok(wc):
    module, mt5 = wc
    mt5.order_check = lambda req: _check_result(retcode=10009, margin_free=-1.0)
    assert module._build_order_check_reply(_order())["verdict"] == "reject"


def test_verdict_reject_invalid_stops(wc):
    module, mt5 = wc
    mt5.order_check = lambda req: _check_result(
        retcode=10016, margin_free=9000.0, comment="Invalid stops")
    assert module._build_order_check_reply(_order())["verdict"] == "reject"


def test_verdict_transient_market_closed(wc):
    module, mt5 = wc
    mt5.order_check = lambda req: _check_result(
        retcode=10018, margin_free=9000.0, comment="Market closed")
    assert module._build_order_check_reply(_order())["verdict"] == "transient"


def test_verdict_transient_when_no_tick(wc):
    module, mt5 = wc
    mt5.symbol_info_tick = lambda sym: None
    mt5.order_check = lambda req: _check_result()
    assert module._build_order_check_reply(_order())["verdict"] == "transient"


def test_verdict_reject_when_algo_disabled(wc):
    module, mt5 = wc
    mt5.terminal_info = lambda: types.SimpleNamespace(trade_allowed=False)
    mt5.order_check = lambda req: _check_result()
    assert module._build_order_check_reply(_order())["verdict"] == "reject"


def test_bad_params_reject(wc):
    module, _ = wc
    out = module._build_order_check_reply({"ticker": "EURUSD", "signal": "LONG", "lots": "x"})
    assert out["verdict"] == "reject"


# ── Issue 7: USD→account-currency rate ────────────────────────────────────────

def test_usd_rate_is_one_for_usd_account(wc):
    module, _ = wc
    assert module._usd_to_account_rate("USD") == 1.0
    assert module._usd_to_account_rate("usd") == 1.0


def test_usd_rate_derived_for_sgd_account(wc, monkeypatch):
    module, _ = wc
    # EURUSD: tick_value(SGD)=1.35, tick_size=0.00001, contract=100000
    # rate = 1.35 / (0.00001 * 100000) = 1.35
    monkeypatch.setattr(module, "_contract_info",
                        lambda sym: (0.00001, 100000.0, 0.00001, 1.35, 5))
    assert module._usd_to_account_rate("SGD") == 1.35


def test_usd_rate_falls_back_to_one_on_error(wc, monkeypatch):
    module, _ = wc
    def _boom(sym):
        raise RuntimeError("no symbol")
    monkeypatch.setattr(module, "_contract_info", _boom)
    assert module._usd_to_account_rate("SGD") == 1.0
