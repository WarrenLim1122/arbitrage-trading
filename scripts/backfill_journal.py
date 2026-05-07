"""
Manual journal backfill for a specific closed trade.

Usage (on VPS #2, from C:\\arbitrage):
    uv run python scripts/backfill_journal.py

Hardcoded for the missed XAUUSD TP trade:
    Ticket: 8520846485  |  2026-05-07 01:15 UTC  |  LONG 0.20 lots
    Entry: 4709.81  SL: 4685.13  TP: 4716.36
"""

import os
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import MetaTrader5 as mt5

MT5_LOGIN    = int(os.getenv("MT5_LOGIN",    "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD",     "")
MT5_SERVER   = os.getenv("MT5_SERVER",       "")
MT5_MAGIC    = int(os.getenv("MT5_MAGIC",    "20250001"))
WORKER_NAME  = os.getenv("WORKER_NAME",      "personal")

_mt5_lock = threading.Lock()


def main():
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    if not mt5.login(MT5_LOGIN, MT5_PASSWORD, MT5_SERVER):
        print(f"MT5 login failed: {mt5.last_error()}")
        mt5.shutdown()
        sys.exit(1)

    account = mt5.account_info()
    print(f"MT5 connected: login={account.login}  server={account.server}")

    # ── Diagnostic: show what history_deals_get actually returns ─────────────
    TICKET       = 8520846485
    OPEN_TIME_UTC = datetime(2026, 5, 7, 1, 15, 4, tzinfo=timezone.utc)
    from_dt = OPEN_TIME_UTC - timedelta(hours=2)
    to_dt   = datetime.now(timezone.utc) + timedelta(seconds=60)

    all_deals = mt5.history_deals_get(from_dt, to_dt) or []
    print(f"\n--- DIAGNOSTIC ---")
    print(f"Query range: {from_dt.isoformat()} → {to_dt.isoformat()}")
    print(f"Total deals in range: {len(all_deals)}")
    pos_deals = [d for d in all_deals if d.position_id == TICKET]
    print(f"Deals matching position_id={TICKET}: {len(pos_deals)}")
    if all_deals and not pos_deals:
        print(f"Sample position_ids returned: {[d.position_id for d in all_deals[:10]]}")
        print(">>> position_id MISMATCH — the ticket filter is wrong, not a timing issue")
    elif not all_deals:
        print(">>> history_deals_get returned EMPTY — timing issue or wrong query range")
    else:
        print(f">>> Deal found! entry={[d.entry for d in pos_deals]} reason={[d.reason for d in pos_deals]}")
    print("------------------\n")
    # ─────────────────────────────────────────────────────────────────────────

    pos_snapshot = {
        "ticket":     8520846485,
        "symbol":     "XAUUSD",
        "type":       0,           # 0=LONG
        "volume":     0.20,
        "price_open": 4709.81,
        "sl":         4685.13,
        "tp":         4716.36,
        "magic":      MT5_MAGIC,
        "open_time":  datetime(2026, 5, 7, 1, 15, 4, tzinfo=timezone.utc),
    }

    print(f"Backfilling journal for ticket {pos_snapshot['ticket']} ({pos_snapshot['symbol']} {pos_snapshot['type']})...")

    from layer3.journal.journaling_worker import handle_closed_position

    result = handle_closed_position(
        mt5_lock=_mt5_lock,
        mt5_account_id=str(MT5_LOGIN),
        worker_name=WORKER_NAME,
        position_ticket=pos_snapshot["ticket"],
        pos_snapshot=pos_snapshot,
    )

    print(f"Done: {result}")
    mt5.shutdown()


if __name__ == "__main__":
    main()
