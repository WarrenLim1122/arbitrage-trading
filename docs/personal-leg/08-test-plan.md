# 08 — Test Plan (write FIRST, per task)

TDD: each file written and watched fail before its module. Exact expected values given. Mirror the
reference test style (`tests/layer2/`).

## §1 `test_reconstruction.py` (T2) — the pinned core
```python
# Phase 2: prop SHORT EURUSD, prop_sl 1.08554, prop_tp 1.08300, prop_lots 18.52, phase 2
g = reconstruct_personal(pair="EURUSD", prop_signal="SHORT",
        prop_sl=1.08554, prop_tp=1.08300, prop_lots=18.52, phase=2,
        price_digits=5, phase_multipliers={"1":0.20,"2":0.70})
assert g["signal"] == "LONG"
assert g["lots"]   == 12.96          # round(18.52*0.70,2)
assert g["sl"]     == 1.08300        # = prop_tp
assert g["tp"]     == 1.08554        # = prop_sl
assert g["ticker"] == "EURUSD"
# Phase 1: prop_lots 1.00, phase 1 → lots round(1.00*0.20,2)=0.20
# prop LONG → personal SHORT (symmetry)
# prop_lots so small that *mult rounds to 0 → {"reject"}
```

## §2 `test_parser.py` (T3)
```python
# structured line
assert parse_prop_message("OPEN|pair=EURUSD|dir=SHORT|entry=1.085|sl=1.08554|tp=1.083|lots=18.52|phase=2",
        sender="propbot", cfg=CFG) == {"type":"open","pair":"EURUSD","dir":"SHORT","entry":1.085,
        "sl":1.08554,"tp":1.083,"lots":18.52,"phase":2}
assert parse_prop_message("CLOSE|pair=EURUSD|reason=TP", "propbot", CFG)["type"] == "close"
assert parse_prop_message("KILL|k=K2|scope=account", "propbot", CFG) == {"type":"kill","k":"K2","scope":"account"}
# sender filter: a message from a non-prop sender → None
assert parse_prop_message("OPEN|pair=EURUSD|...", sender="someone_else", cfg=CFG) is None
# keyword fallback on human text when no structured line; malformed line → None (logged)
```

## §3 `test_follower.py` (T7) — mocked zmq + equity
```python
# open event → exactly ONE ticket pushed, shape per 05 §2, lots/sl/tp from reconstruction
# close event → push_close called for that pair only
# kill K2 (account, permanent) → close-all + halt set
# kill scope=EURUSD → close that pair only
# non-prop sender / follow_enabled=False / not active / permanently_halted → no action
# dedup: pair already open → no second open
# max_open_positions reached → skip
```

## §4 `test_messages.py` (T9)
`msg_hedge_opened(currency="SGD")` contains `SGD ` and no `$`; `msg_position_closed(found=False)` →
`(est.)`, `found=True` → `+SGD ..`/`-SGD ..` (sign before symbol, never `$+`/`$-`); prices carry no
symbol; `msg_reader_disconnected` renders. Audit: a rendered SGD alert contains no `$`.

## §5 `test_dayroll.py` (T8)
`current_day("2026-06-14 10:30" SGT)` == 2026-06-13; `11:30` == 2026-06-14 (day_roll 11:00).
`money(-12.5,"SGD")=="SGD 12.50"`; `"$" not in money(12.5,"SGD")`. `fmt_price`: USDJPY 3dp, EURUSD 5dp,
XAUUSD 2dp, XAGUSD 4dp.

## §6 `test_zmq_client.py` (T4)
Round-trip vs an in-process fake REP; assert the ticket JSON matches `05 §2` exactly; a `push_close(pair)`
emits the expected close instruction; a REQ timeout returns a clean error, not a hang.

## §7 Worker units (T5/T6)
filling-mode order IOC→FOK→RETURN; ticket→MT5 request mapping; deal-window upper bound = now+1day;
`deal_pnl` found=False without DEAL_ENTRY_OUT; force-close by pair; journaling record currency badge.
Live MT5 → CP-2.

## Green bar
`pytest` all green + the `scripts/dry_run_prop_event.py` trace correct (prop OPEN → hedge ticket;
CLOSE → close; K2 → close-all+halt) ⇒ ready for CP-1.
