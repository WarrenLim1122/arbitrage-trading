"""DEMO: how the system prevents an ORPHANED leg, run through REAL code.

Layer 2 (logic_core.py:1560-1595) calls _build_order_check_reply on BOTH legs
BEFORE placing EITHER order. If either verdict == "reject", it places NOTHING —
so one leg can never be left hedging alone. This runs the actual worker
_build_order_check_reply (incl. the session-20 self-healing $0 NO_MONEY guard)
against a mocked MT5, then replays Layer 2's gate decision.
Run:  uv run python scripts/dev-tests/demo_orphan_prevention.py
"""
import os
import sys
import types
from pathlib import Path

_CONSTS = {
    "TRADE_ACTION_DEAL": 1, "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1,
    "ORDER_TIME_GTC": 0, "ORDER_FILLING_IOC": 1,
    "TRADE_RETCODE_DONE": 10009, "TRADE_RETCODE_NO_MONEY": 10019,
    "TRADE_RETCODE_MARKET_CLOSED": 10018, "TRADE_RETCODE_REQUOTE": 10004,
    "TRADE_RETCODE_PRICE_OFF": 10021, "TRADE_RETCODE_PRICE_CHANGED": 10020,
    "TRADE_RETCODE_INVALID_STOPS": 10016,
}
mt5 = types.ModuleType("MetaTrader5")
for k, v in _CONSTS.items():
    setattr(mt5, k, v)
mt5.last_error        = lambda: (0, "ok")
mt5.symbol_info_tick  = lambda s: types.SimpleNamespace(ask=2000.1, bid=2000.0, time=0)
mt5.terminal_info     = lambda: types.SimpleNamespace(trade_allowed=True)
mt5.account_info      = lambda: types.SimpleNamespace(login=20047930, margin_free=50_000.0)
mt5.order_calc_margin = lambda otype, sym, lots, px: 5_000.0   # real margin need
mt5.order_check       = None  # set per-case
sys.modules["MetaTrader5"] = mt5

os.environ.update({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
                   "MT5_LOGIN": "20047930", "MT5_PASSWORD": "p", "MT5_SERVER": "s"})
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import layer3._worker_core as wc
wc._ensure_connected = lambda: None
wc._resolve_symbol   = lambda c: c
wc._get_filling_mode = lambda r: 1


def chk(**kw):
    base = dict(retcode=10009, margin=5000.0, margin_free=45000.0,
                balance=50000.0, equity=50000.0, comment="Done")
    base.update(kw)
    return types.SimpleNamespace(**base)


ORDER = {"ticker": "XAUUSD", "signal": "LONG", "lots": 0.27, "sl": 1980.0, "tp": 1980.0}


def layer2_gate(prop_verdict, pers_verdict):
    """Mirror logic_core.py:1573 — place nothing if EITHER leg rejects."""
    blocked = prop_verdict == "reject" or pers_verdict == "reject"
    return "PLACE NOTHING (no orphan)" if blocked else "place BOTH legs"


print("============ ORPHAN-PREVENTION SCENARIOS ============\n")

print("A. Both legs affordable → both placed")
mt5.order_check = lambda req: chk(retcode=10009, margin_free=45000.0)
v = wc._build_order_check_reply(ORDER)["verdict"]
print(f"   prop verdict={v}  pers verdict={v}  → Layer 2: {layer2_gate(v, v)}\n")

print("B. One leg genuinely out of money (margin_free NEGATIVE) → blocks BOTH")
mt5.order_check = lambda req: chk(retcode=10019, margin=60000.0,
                                  margin_free=-10000.0, comment="No money")
v_bad = wc._build_order_check_reply(ORDER)["verdict"]
print(f"   failing leg verdict={v_bad}  → Layer 2: {layer2_gate(v_bad, 'ok')}")
print("   (real shortfall → reject stands → the good leg is NOT orphaned)\n")

print("C. Degenerate $0 NO_MONEY read (session-20 self-heal): order_check says")
print("   NO_MONEY with margin_free EXACTLY 0.0, but live account_info free=$50k")
print("   and real margin need=$5k → downgrade reject→transient (retry, no false block)")
mt5.order_check = lambda req: chk(retcode=10019, margin=0.0,
                                  margin_free=0.0, comment="No money")
v_heal = wc._build_order_check_reply(ORDER)["verdict"]
print(f"   verdict={v_heal}  → Layer 2: {layer2_gate(v_heal, 'ok')}")
print("   (bogus $0 no longer kills a fine trade — yet a REAL shortfall in B still does)\n")

print("D. Market closed / requote → transient (NOT reject) → normal retry, no block")
mt5.order_check = lambda req: chk(retcode=10018, margin_free=45000.0, comment="Market closed")
v_t = wc._build_order_check_reply(ORDER)["verdict"]
print(f"   verdict={v_t}  → Layer 2: {layer2_gate(v_t, 'ok')}\n")

print("E. Invalid stops (SL too close) → reject → blocks BOTH (prevents a 1-leg fill)")
mt5.order_check = lambda req: chk(retcode=10016, margin_free=45000.0, comment="Invalid stops")
v_s = wc._build_order_check_reply(ORDER)["verdict"]
print(f"   verdict={v_s}  → Layer 2: {layer2_gate(v_s, 'ok')}\n")

print("Plus a runtime fallback: the orphan watcher (logic_core _CLOSE_WAIT_SECONDS)")
print("force-closes any leg if only one ever opens — defence in depth.")
