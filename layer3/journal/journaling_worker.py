"""
Full journaling pipeline for a completed MT5 TP/SL trade.

Called from _worker_core._journal_closed_position() in a daemon thread.
Must never raise — all exceptions are caught and logged.

Flow:
  1. Fetch deal history from MT5 to find entry + exit deal
  2. Determine close reason (TP / SL / other)
  3. Extract actual MT5 values (price, PnL, commission, swap)
  4. Generate Python RR chart screenshot
  5. Upload screenshot to Firebase Storage
  6. Write trade payload to Firestore
  7. On Firestore failure: enqueue payload for retry
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PENDING_DEALS_PATH = Path(__file__).parent.parent.parent / "journal_pending_deals.jsonl"
_pending_lock = __import__("threading").Lock()


def _enqueue_pending_deal(ticket: int, pos_snapshot: dict, mt5_account_id: str, worker_name: str) -> None:
    symbol = pos_snapshot.get("symbol", "?")
    now    = datetime.now(timezone.utc).isoformat()
    entry  = {
        "ticket":           ticket,
        "symbol":           symbol,
        "mt5_account_id":   mt5_account_id,
        "worker_name":      worker_name,
        "queued_at":        now,
        "last_notified_at": now,
        "snapshot": {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in pos_snapshot.items()
        },
    }
    try:
        with _pending_lock:
            with _PENDING_DEALS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        logger.info("Journal: deal queued for later retry (ticket=%d %s)", ticket, symbol)
        # Initial Telegram notification (VPS #2 needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)
        from .pending_deals_queue import send_queued_notification
        send_queued_notification(symbol, ticket)
    except Exception as exc:
        logger.error("Failed to enqueue pending deal (ticket=%d): %s", ticket, exc)

JOURNAL_ENABLED        = os.getenv("FIREBASE_JOURNAL_ENABLED",   "false").lower() == "true"
JOURNAL_TP_SL_ONLY     = os.getenv("SCREENSHOT_ONLY_FOR_TP_SL",  "false").lower() == "true"
JOURNAL_ACCOUNT_TYPE   = os.getenv("JOURNAL_ACCOUNT_TYPE",       "demo")   # demo|live|prop
JOURNAL_BROKER         = os.getenv("JOURNAL_BROKER",              "")
FIREBASE_BOT_NAME      = os.getenv("FIREBASE_BOT_NAME",          "HedgeHog Bot")
FIREBASE_STRATEGY_NAME = os.getenv("FIREBASE_STRATEGY_NAME",     "Arbitrage Trading")
FIREBASE_JOURNAL_USER_ID = os.getenv("FIREBASE_JOURNAL_USER_ID", "")

# MT5 DEAL_ENTRY_* and DEAL_REASON_* constants (mirror MT5 spec for offline test)
_DEAL_ENTRY_IN  = 0
_DEAL_ENTRY_OUT = 1


def _get_deals(mt5_lock, position_ticket: int, open_time: datetime):
    """Return (entry_deals, exit_deals) for the given position ticket."""
    import MetaTrader5 as mt5

    # Use the earlier of (open_time - 2h) and (now - 6h) as from_dt.
    # MT5 demo servers may report position.time in server-local timezone rather
    # than as a pure UTC Unix timestamp, causing open_time to appear hours ahead
    # of the actual close time. The min() guard ensures from_dt is always before to_dt.
    safe_from = datetime.now(timezone.utc) - timedelta(hours=6)
    from_dt   = min(open_time - timedelta(hours=2), safe_from)
    to_dt     = datetime.now(timezone.utc) + timedelta(seconds=60)

    with mt5_lock:
        all_deals = mt5.history_deals_get(from_dt, to_dt) or []

    pos_deals = [d for d in all_deals if d.position_id == position_ticket]

    if not pos_deals:
        # Distinguish between "API returned nothing" vs "deals exist but position_id didn't match"
        if not all_deals:
            logger.info(
                "Journal _get_deals: history_deals_get returned 0 deals total "
                "(range %s → %s) for position=%d",
                from_dt.isoformat(), to_dt.isoformat(), position_ticket,
            )
        else:
            sample_ids = [d.position_id for d in all_deals[:10]]
            logger.warning(
                "Journal _get_deals: %d deals returned but NONE matched position_id=%d. "
                "Sample position_ids in range: %s — possible position_id mismatch.",
                len(all_deals), position_ticket, sample_ids,
            )

    entry_deals  = [d for d in pos_deals if d.entry == mt5.DEAL_ENTRY_IN]
    exit_deals   = [d for d in pos_deals if d.entry == mt5.DEAL_ENTRY_OUT]
    return entry_deals, exit_deals


def _take_screenshot_immediate(
    mt5_lock,
    position_ticket: int,
    pos_snapshot: dict,
    mt5_account_id: str,
) -> dict:
    """
    Capture screenshot immediately at close detection — no deal history required.

    Uses position snapshot data (entry, SL, TP, direction) and the close time /
    price that _position_close_watcher stamped into the snapshot within 5 s of
    the actual close.  Net P&L is unknown at this stage so it is omitted from
    the chart badge.

    Returns screenshot_fields dict ready to merge into the Firestore payload.
    """
    import MetaTrader5 as mt5

    symbol       = pos_snapshot.get("symbol", "")
    direction    = "LONG" if pos_snapshot.get("type") == 0 else "SHORT"
    entry_price  = pos_snapshot.get("price_open", 0.0)
    sl_price     = pos_snapshot.get("sl",         0.0) or 0.0
    tp_price     = pos_snapshot.get("tp",         0.0) or 0.0
    open_time    = pos_snapshot.get("open_time",  datetime.now(timezone.utc))
    volume       = pos_snapshot.get("volume",     0.0)
    close_reason = pos_snapshot.get("close_reason_override", "MARKET")

    # close_time: stamped by position watcher — accurate to within ~5 s
    raw_ct = pos_snapshot.get("close_time_detected")
    if isinstance(raw_ct, str):
        try:
            close_time = datetime.fromisoformat(raw_ct)
        except ValueError:
            close_time = datetime.now(timezone.utc)
    elif isinstance(raw_ct, datetime):
        close_time = raw_ct
    else:
        close_time = datetime.now(timezone.utc)

    # close_price: tick price stamped by position watcher (position just closed)
    close_price = pos_snapshot.get("close_price_est")

    # If no tick price was captured, try one quick deal query (works on real brokers)
    if close_price is None:
        try:
            safe_from = min(
                open_time - timedelta(hours=2),
                datetime.now(timezone.utc) - timedelta(hours=6),
            )
            to_dt = datetime.now(timezone.utc) + timedelta(seconds=60)
            with mt5_lock:
                deals = mt5.history_deals_get(safe_from, to_dt) or []
            exits = [
                d for d in deals
                if d.position_id == position_ticket and d.entry == mt5.DEAL_ENTRY_OUT
            ]
            if exits:
                close_price = exits[-1].price
                close_time  = datetime.fromtimestamp(exits[-1].time, tz=timezone.utc)
                logger.info(
                    "Journal: immediate deal query succeeded for ticket=%d close_price=%s",
                    position_ticket, close_price,
                )
        except Exception:
            pass

    # Final fallback: infer from TP/SL proximity (handles MetaQuotes Demo with no tick)
    if close_price is None:
        close_price = tp_price if tp_price else sl_price

    # Infer outcome from close price vs entry
    if direction == "LONG":
        outcome = "WIN" if close_price >= entry_price else "LOSS"
    else:
        outcome = "WIN" if close_price <= entry_price else "LOSS"

    rr_ratio = None
    if sl_price and tp_price and entry_price:
        risk   = abs(entry_price - sl_price)
        reward = abs(entry_price - tp_price)
        if risk > 0:
            rr_ratio = round(reward / risk, 2)

    try:
        from .screenshot_capture import capture_outcome_screenshot
        fields = capture_outcome_screenshot(
            symbol=symbol, direction=direction,
            entry_price=entry_price, sl_price=sl_price, tp_price=tp_price,
            close_price=close_price, close_time=close_time, open_time=open_time,
            outcome=outcome,
            net_pnl=None,   # unknown until deal history — omitted from chart badge
            volume=volume,
            ticket=position_ticket, account_type=JOURNAL_ACCOUNT_TYPE,
            mt5_account_id=mt5_account_id, close_reason=close_reason,
            mt5_lock=mt5_lock, rr_ratio=rr_ratio,
        )
        logger.info(
            "Journal: immediate screenshot %s for ticket=%d",
            fields.get("outcomeScreenshotStatus", "failed"), position_ticket,
        )
        return fields
    except Exception as exc:
        logger.error("Immediate screenshot failed (ticket=%d): %s", position_ticket, exc)
        return {"outcomeScreenshotStatus": "failed", "outcomeScreenshotSource": "python_script"}


def handle_closed_position(
    mt5_lock,
    mt5_account_id: str,
    worker_name: str,       # "personal" | "prop"
    position_ticket: int,
    pos_snapshot: dict,
    skip_retry: bool = False,   # True when called from pending-deals retry thread
) -> Optional[dict]:
    """
    Run the journaling pipeline for one closed position.
    Returns a small status dict (journal_status, screenshot_status) or None on skip.
    Safe to call from a daemon thread — all exceptions caught internally.

    Phase 1 (immediate): screenshot captured from snapshot + tick data — no deal history needed.
    Phase 2 (may be delayed): deal history fetched; Firestore written with Phase 1 screenshot URL.
    Pending queue carries the Phase 1 screenshot forward so it is never lost.
    """
    if not JOURNAL_ENABLED:
        return None

    import MetaTrader5 as mt5

    symbol    = pos_snapshot.get("symbol", "")
    open_time = pos_snapshot.get("open_time", datetime.now(timezone.utc))

    try:
        # ── Phase 1: Immediate screenshot ────────────────────────────────
        # Only captured once — pending-queue retries reuse the stored result.
        if "_screenshot_fields" not in pos_snapshot:
            pos_snapshot["_screenshot_fields"] = _take_screenshot_immediate(
                mt5_lock, position_ticket, pos_snapshot, mt5_account_id
            )

        # ── Phase 2: Deal history ────────────────────────────────────────
        entry_deals, exit_deals = _get_deals(mt5_lock, position_ticket, open_time)

        if not exit_deals and not skip_retry:
            import time
            # MT5 history can lag several minutes after close (MetaQuotes Demo in particular
            # can take >2 min to sync deal history). Extended backoff covers ~7 min total.
            for attempt, wait in enumerate([5, 10, 20, 40, 60, 120, 180], start=1):
                logger.info(
                    "Journal: no exit deal yet for position %d (attempt %d/7) — retrying in %ds",
                    position_ticket, attempt, wait,
                )
                time.sleep(wait)
                entry_deals, exit_deals = _get_deals(mt5_lock, position_ticket, open_time)
                if exit_deals:
                    break

        if not exit_deals:
            if skip_retry:
                # Called from pending-deals retry thread — not found yet, stay in queue
                logger.info(
                    "Journal: no exit deal for position %d (pending retry) — will try again later",
                    position_ticket,
                )
            else:
                # All 7 inline retries failed — save to persistent queue for later retry.
                # pos_snapshot now includes _screenshot_fields so the screenshot is preserved.
                _enqueue_pending_deal(position_ticket, pos_snapshot, mt5_account_id, worker_name)
                logger.warning(
                    "Journal: no exit deal for position %d after all retries — queued for later retry",
                    position_ticket,
                )
            return None

        exit_deal  = exit_deals[-1]
        entry_deal = entry_deals[0] if entry_deals else None

        # ── 2. Close reason ───────────────────────────────────────────────
        reason_map = {
            mt5.DEAL_REASON_TP:     "TP",
            mt5.DEAL_REASON_SL:     "SL",
            mt5.DEAL_REASON_EXPERT: "BOT_LOGIC",
            mt5.DEAL_REASON_MOBILE: "MANUAL",
            mt5.DEAL_REASON_CLIENT: "MANUAL",
        }
        close_reason = pos_snapshot.get("close_reason_override") or reason_map.get(exit_deal.reason, "MANUAL")

        # ── 3. Actual MT5 values ──────────────────────────────────────────
        direction   = "LONG" if pos_snapshot.get("type") == 0 else "SHORT"
        entry_price = entry_deal.price if entry_deal else pos_snapshot.get("price_open", 0.0)
        close_price = exit_deal.price
        volume      = round(exit_deal.volume, 2)
        close_time  = datetime.fromtimestamp(exit_deal.time, tz=timezone.utc)

        all_pos_deals = entry_deals + exit_deals
        gross_pnl  = sum(d.profit     for d in all_pos_deals)
        commission = sum(d.commission for d in all_pos_deals)
        swap_total = sum(d.swap       for d in all_pos_deals)
        net_pnl    = gross_pnl + commission + swap_total

        outcome = "WIN" if net_pnl >= 0 else "LOSS"

        sl_price = pos_snapshot.get("sl", 0.0) or 0.0
        tp_price = pos_snapshot.get("tp", 0.0) or 0.0

        rr_ratio = None
        if sl_price and tp_price and entry_price:
            risk   = abs(entry_price - sl_price)
            reward = abs(entry_price - tp_price)
            if risk > 0:
                rr_ratio = round(reward / risk, 2)

        # ── 4. Screenshot — use Phase 1 result (already captured immediately) ──
        screenshot_fields = pos_snapshot.get("_screenshot_fields", {
            "outcomeScreenshotStatus": "failed",
            "outcomeScreenshotSource": "python_script",
        })

        ss_status_raw = screenshot_fields.get("outcomeScreenshotStatus", "failed")
        screenshot_status = (
            "screenshot ✅" if ss_status_raw == "success" else "screenshot failed ⚠️"
        )

        # ── 5. Build Firestore payload ────────────────────────────────────
        from .firebase_journal import (
            build_document_id, derive_market_type,
        )

        now_iso   = datetime.now(timezone.utc).isoformat()
        open_iso  = (open_time if isinstance(open_time, datetime) else datetime.now(timezone.utc)).isoformat()
        close_iso = close_time.isoformat()
        doc_id    = build_document_id(JOURNAL_ACCOUNT_TYPE, mt5_account_id, position_ticket)

        payload = {
            # ── Identity ──────────────────────────────────────────────────
            "id":           doc_id,
            "userId":       FIREBASE_JOURNAL_USER_ID,
            "source":       "bot",
            "botName":      FIREBASE_BOT_NAME,
            "strategyName": FIREBASE_STRATEGY_NAME,
            "accountType":  JOURNAL_ACCOUNT_TYPE,
            "broker":       JOURNAL_BROKER,
            "mt5AccountId": mt5_account_id,
            "ticket":       position_ticket,
            "magicNumber":  pos_snapshot.get("magic"),

            # ── Trade info ────────────────────────────────────────────────
            "marketType":  derive_market_type(symbol),
            "symbol":      symbol,
            "pair":        symbol,                          # legacy field
            "direction":   direction,
            "position":    direction.capitalize(),          # legacy: "Long" / "Short"
            "volume":      volume,
            "entryPrice":  entry_price,
            "closePrice":  close_price,
            "stopLoss":    sl_price,
            "takeProfit":  tp_price,
            "openTime":    open_iso,
            "closeTime":   close_iso,
            "date":        close_iso,                       # legacy date field
            "closeReason": close_reason,

            # ── PnL (netPnl has highest precedence per schema) ────────────
            "grossPnl":       round(gross_pnl,  2),
            "commission":     round(commission, 2),
            "swap":           round(swap_total, 2),
            "netPnl":         round(net_pnl,    2),
            "pnlAmount":      round(net_pnl,    2),         # legacy field
            "accountCurrency": "USD",

            # ── Risk/Reward ───────────────────────────────────────────────
            "rrRatio":         rr_ratio,
            "estimatedRisk":   round(abs(entry_price - sl_price) * volume * 100, 2) if sl_price else None,
            "estimatedReward": round(abs(entry_price - tp_price) * volume * 100, 2) if tp_price else None,

            # ── Outcome ───────────────────────────────────────────────────
            "outcome": outcome,
            "tags":    ["bot", "arbitrage", worker_name],
            "notes":   "",

            # ── Screenshots (merged from capture pipeline) ─────────────────
            **screenshot_fields,

            # ── Metadata ──────────────────────────────────────────────────
            "createdAt":  now_iso,
            "updatedAt":  now_iso,
            "importedAt": now_iso,

            # ── Raw MT5 data for debugging ────────────────────────────────
            "rawMt5Data": {
                "positionTicket":  position_ticket,
                "entryDealTicket": entry_deal.ticket if entry_deal else None,
                "exitDealTicket":  exit_deal.ticket,
                "exitDealReason":  exit_deal.reason,
                "exitDealTime":    close_iso,
            },
        }

        # ── 6. Write to Firestore ─────────────────────────────────────────
        from .firebase_journal import write_trade
        from .retry_queue import enqueue

        ok = write_trade(payload)
        if ok:
            journal_status = "saved"
        else:
            enqueue(payload)
            journal_status = "failed, queued for retry ⚠️"

        logger.info(
            "Journal [%s] ticket=%d  %s  %s  %.2f lots  net_pnl=%.2f  %s  %s",
            JOURNAL_ACCOUNT_TYPE, position_ticket, outcome, symbol,
            volume, net_pnl, journal_status, screenshot_status,
        )

        return {"journal_status": journal_status, "screenshot_status": screenshot_status}

    except Exception as exc:
        logger.error(
            "Journal pipeline error (ticket=%d): %s", position_ticket, exc, exc_info=True
        )
        return None
