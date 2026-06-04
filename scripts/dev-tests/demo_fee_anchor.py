"""DEMO: run the REAL worker fee code against a mocked personal account.

Reproduces the Telegram /equity screenshot (Personal: Balance SGD 6,500,
Trading Fee SGD -12.40) using layer3/_worker_core.py UNCHANGED, then proves
what makes the -12.40 appear vs. disappear.
Run:  uv run python scripts/dev-tests/demo_fee_anchor.py
"""
import os
import sys
import types
from pathlib import Path

MT5_LOGIN = "448196"  # live personal account (SGD)

# ---- mock MetaTrader5 BEFORE importing the worker -------------------------
_CONSTS = {
    "DEAL_TYPE_BALANCE": 2, "DEAL_ENTRY_OUT": 1, "DEAL_ENTRY_IN": 0,
    "TRADE_ACTION_DEAL": 1, "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1,
    "ORDER_TIME_GTC": 0, "ORDER_FILLING_IOC": 1,
    "ACCOUNT_TRADE_MODE_DEMO": 0, "ACCOUNT_TRADE_MODE_REAL": 2,
}
mt5 = types.ModuleType("MetaTrader5")
for k, v in _CONSTS.items():
    setattr(mt5, k, v)

# Deal history: one 6,500 deposit + a trade whose gross P&L = +12.40 but whose
# commission/swap = -12.40, so the account is back at 6,500. This is exactly the
# shape `balance - Σprofit` was designed to reconcile.
_DEALS = [
    types.SimpleNamespace(type=2, profit=6500.0, commission=0.0, swap=0.0, entry=0),    # deposit
    types.SimpleNamespace(type=0, profit=12.40, commission=-10.00, swap=-2.40, entry=1),  # closed trade
]
mt5.account_info   = lambda: types.SimpleNamespace(
    login=int(MT5_LOGIN), balance=6500.0, equity=6500.0, profit=0.0,
    currency="SGD", server="FusionMarkets-Live", name="Chee Heng Lai 006")
mt5.terminal_info  = lambda: types.SimpleNamespace(trade_allowed=True)
mt5.positions_get  = lambda *a, **k: []
mt5.history_deals_get = lambda _from, _to: list(_DEALS)
mt5.symbol_info_tick  = lambda s: types.SimpleNamespace(ask=1.1, bid=1.0, time=0)
sys.modules["MetaTrader5"] = mt5

os.environ.update({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
                   "MT5_LOGIN": MT5_LOGIN, "MT5_PASSWORD": "p", "MT5_SERVER": "s"})

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import layer3._worker_core as wc

# neutralise FX/contract lookups that need a real symbol feed
wc._contract_info = lambda c: (0.00001, 100000.0, 0.00001, 1.0, 5)
wc._usd_to_account_rate = lambda cur: 1.0

anchor_file = wc._fee_anchor_path()
print(f"anchor file path : {anchor_file}")
print(f"anchor exists?   : {anchor_file.exists()}\n")

if anchor_file.exists():
    anchor_file.unlink()

print("=== SCENARIO A: no anchor file (account just switched / never reset) ===")
r = wc._build_equity_reply("EURUSD", want_fee=True)
print(f"  loaded anchor      : {wc._load_fee_anchor():.2f}")
print(f"  trading_fee_total  : {r.get('trading_fee_total')}   <-- the -12.40 in Telegram")
print(f"  deposit_total      : {r.get('deposit_total')}\n")

print("=== SCENARIO B: fire reset_fee_anchor (what /phase1 /phase2 /changepropfirm do) ===")
reset = wc._build_reset_fee_anchor_reply()
print(f"  reset reply        : {reset}")
r2 = wc._build_equity_reply("EURUSD", want_fee=True)
print(f"  loaded anchor      : {wc._load_fee_anchor():.2f}")
print(f"  trading_fee_total  : {r2.get('trading_fee_total')}   <-- now per-cycle = 0.00\n")

print("=== SCENARIO C: a NEW trade closes after the reset (cost SGD -3.00) ===")
_DEALS.append(types.SimpleNamespace(type=0, profit=5.00, commission=-2.50, swap=-0.50, entry=1))
mt5.account_info = lambda: types.SimpleNamespace(
    login=int(MT5_LOGIN), balance=6502.0, equity=6502.0, profit=0.0,
    currency="SGD", server="FusionMarkets-Live", name="Chee Heng Lai 006")
r3 = wc._build_equity_reply("EURUSD", want_fee=True)
print(f"  trading_fee_total  : {r3.get('trading_fee_total')}   <-- only this cycle's -3.00 fee\n")

anchor_file.unlink(missing_ok=True)
print("Cleaned up demo anchor file.")
