"""Position Closed Telegram alert — presentation contract.

Guards the end-to-end promise: once Layer 3's deal-history query window was
widened past the trade server's UTC offset (see _get_deals / _build_deal_pnl_reply),
the exit deal surfaces on the FIRST query, so _detect_closes flushes with
`deal['found'] == True`. This test pins what the user then sees in Telegram:

  - found path  → real net P&L, real exit price, TP/SL reason + title, Trading
                  Fee row, and NO "(est.)" anywhere.
  - missing path→ the "(est.)" fallback (only reachable when MT5 never surfaced
                  the deal within _CLOSE_DEAL_TIMEOUT).

These are pure-function assertions on msg_position_closed — no network/MT5.
"""
from layer2.telegram_handlers import msg_position_closed


# Personal SHORT (type 1), Prop LONG (type 0) — the standard hedge layout.
_PERS_POS = {
    "ticket": 179074702, "type": 1, "volume": 0.02,
    "price_open": 4482.62, "sl": 4523.27, "tp": 4476.27, "profit": 20.71,
}
_PROP_POS = {
    "ticket": 370231349, "type": 0, "volume": 0.03,
    "price_open": 4483.49, "sl": 4476.27, "tp": 4523.27, "profit": -25.11,
}


def _found_deal(*, net, gross, commission, swap, close_price, reason):
    return {
        "found": True, "account_mode": "real", "close_reason": reason,
        "net_pnl": net, "gross_pnl": gross, "commission": commission,
        "swap": swap, "close_price": close_price,
    }


def _msg(pers_deal, prop_deal, account_mode="real"):
    return msg_position_closed(
        symbol="XAUUSD",
        pers_pos_data=_PERS_POS, prop_pos_data=_PROP_POS,
        pers_deal=pers_deal, prop_deal=prop_deal,
        curr_pers=[], curr_prop=[],
        pers_currency="SGD", pers_eq_str="SGD 554.11", prop_eq_str="$4,895.94",
        is_news_close=False, account_mode=account_mode,
    )


# ── found path: the post-fix happy path the user expects within 30s ──────────

def test_found_deals_render_no_est_and_real_values():
    pers = _found_deal(net=20.71, gross=21.30, commission=-0.49, swap=-0.10,
                       close_price=4476.27, reason="TP")
    prop = _found_deal(net=-25.11, gross=-24.40, commission=-0.61, swap=-0.10,
                       close_price=4476.27, reason="TP")
    out = _msg(pers, prop)

    # The whole point of the fix: never an estimate when the deal is found.
    assert "(est.)" not in out
    assert "Deal data unavailable" not in out

    # Title reflects the real close reason (TP), not a P&L-sign guess.
    assert "XAUUSD — Take Profit" in out

    # Real net P&L in each side's currency (personal SGD, prop USD).
    # _msg_signed_money puts the sign BEFORE the currency: "+SGD 20.71" / "-$25.11".
    assert "+SGD 20.71" in out
    assert "-$25.11" in out

    # Real exit price from the deal, and the Trading Fee row (commission + swap).
    assert "4476.27" in out
    assert "Trading Fee" in out


def test_found_stop_loss_sets_red_title():
    pers = _found_deal(net=-94.68, gross=-93.0, commission=-1.5, swap=-0.18,
                       close_price=4523.27, reason="SL")
    prop = _found_deal(net=120.0, gross=121.0, commission=-0.8, swap=-0.20,
                       close_price=4523.27, reason="SL")
    out = _msg(pers, prop)
    assert "XAUUSD — Stop Loss" in out
    assert "(est.)" not in out


# ── missing path: the fallback we want to almost never hit anymore ───────────

def test_missing_deal_falls_back_to_est():
    out = _msg(pers_deal={"found": False, "account_mode": "real"},
               prop_deal={"found": False, "account_mode": "real"},
               account_mode="real")
    assert "(est.)" in out
    assert "Deal data unavailable" in out  # real-account footer
