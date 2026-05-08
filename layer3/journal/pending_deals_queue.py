"""
Persistent retry queue for journal entries where MT5 deal history wasn't available.

When the inline 7-attempt retry in journaling_worker exhausts without finding the exit
deal, the position snapshot is saved here. A background thread retries every
RETRY_INTERVAL seconds, calling handle_closed_position with skip_retry=True (one attempt,
no sleep). Entries older than MAX_AGE_HOURS are dropped.

Queue file: journal_pending_deals.jsonl (project root — .gitignored)

Telegram notifications (requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in VPS #2 .env):
  - On first enqueue: "📋 Journal Queued — {symbol}"
  - Every NOTIFY_INTERVAL_H hours while still pending: "⏳ Journal Still Pending"
  - On success from pending queue: "✅ Journal Recovered"
  - At 24h limit before drop: "⚠️ Journal Failed"
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PENDING_QUEUE_PATH = Path(__file__).parent.parent.parent / "journal_pending_deals.jsonl"
RETRY_INTERVAL    = 7200  # 2 hours between retry sweeps
MAX_AGE_HOURS     = 24
NOTIFY_INTERVAL_H = 3     # hours between Telegram reminders per entry

_TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

_lock = threading.Lock()


def _send_telegram(msg: str) -> None:
    if not _TELEGRAM_TOKEN or not _TELEGRAM_CHAT:
        logger.debug("Telegram creds not set on VPS #2 — skipping journal notification")
        return
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": _TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        logger.error("Journal Telegram notification failed: %s", exc)


def send_queued_notification(symbol: str, ticket: int) -> None:
    _send_telegram(
        f"📋 <b>Journal Queued — {symbol}</b>\n\n"
        f"Ticket #{ticket} — MT5 deal history not yet available.\n"
        f"Will retry every 10 min for up to 24h."
    )


def process_queue(mt5_lock, mt5_account_id: str, worker_name: str) -> None:
    """Attempt to journal all pending deals. Sends Telegram reminders per entry."""
    if not _PENDING_QUEUE_PATH.exists():
        return

    from .journaling_worker import handle_closed_position  # lazy — avoid circular at module load

    try:
        with _lock:
            raw = _PENDING_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
            _PENDING_QUEUE_PATH.write_text("", encoding="utf-8")   # clear atomically
    except Exception as exc:
        logger.error("Pending deals queue read error: %s", exc)
        return

    lines = [ln.strip() for ln in raw if ln.strip()]
    if not lines:
        return

    logger.info("Pending deals: processing %d queued entries", len(lines))
    remaining = []

    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        ticket  = entry.get("ticket", 0)
        symbol  = entry.get("symbol", entry.get("snapshot", {}).get("symbol", "?"))
        now_utc = datetime.now(timezone.utc)

        # Parse timestamps
        queued_at        = _parse_dt(entry.get("queued_at", ""), now_utc)
        last_notified_at = _parse_dt(entry.get("last_notified_at", entry.get("queued_at", "")), now_utc)

        age_h            = (now_utc - queued_at).total_seconds() / 3600
        since_notify_h   = (now_utc - last_notified_at).total_seconds() / 3600

        # Drop entries at or past 24h limit — send warning first
        if age_h >= MAX_AGE_HOURS:
            logger.warning("Pending deal expired (%.1fh) — dropping ticket=%d %s", age_h, ticket, symbol)
            _send_telegram(
                f"⚠️ <b>Journal Failed — {symbol}</b>\n\n"
                f"Ticket #{ticket} could not be journaled after {MAX_AGE_HOURS}h.\n"
                f"Deal history was never available on MetaQuotes Demo.\n"
                f"Please add this trade manually to the journal if needed."
            )
            continue

        # Reconstruct snapshot with open_time as datetime
        snapshot = dict(entry.get("snapshot", {}))
        if "open_time" in snapshot and isinstance(snapshot["open_time"], str):
            try:
                snapshot["open_time"] = datetime.fromisoformat(snapshot["open_time"])
            except ValueError:
                snapshot["open_time"] = now_utc

        result = handle_closed_position(
            mt5_lock=mt5_lock,
            mt5_account_id=entry.get("mt5_account_id", mt5_account_id),
            worker_name=entry.get("worker_name", worker_name),
            position_ticket=ticket,
            pos_snapshot=snapshot,
            skip_retry=True,
        )

        if result is not None:
            logger.info("Pending deal journaled successfully: ticket=%d %s (%.1fh after close)", ticket, symbol, age_h)
            _send_telegram(
                f"✅ <b>Journal Recovered — {symbol}</b>\n\n"
                f"Ticket #{ticket} successfully journaled.\n"
                f"(Deal history appeared {age_h:.1f}h after trade close.)"
            )
            continue

        # Still pending — check if 3-hour reminder is due
        updated_entry = dict(entry)
        updated_entry["symbol"] = symbol   # ensure symbol is stored for future sweeps

        if since_notify_h >= NOTIFY_INTERVAL_H:
            updated_entry["last_notified_at"] = now_utc.isoformat()
            _send_telegram(
                f"⏳ <b>Journal Still Pending — {symbol}</b>\n\n"
                f"Ticket #{ticket} not yet journaled ({age_h:.0f}h elapsed).\n"
                f"Retrying automatically every 10 min.\n"
                f"Will give up at {MAX_AGE_HOURS}h."
            )

        remaining.append(json.dumps(updated_entry, default=str))

    if remaining:
        with _lock:
            with _PENDING_QUEUE_PATH.open("a", encoding="utf-8") as fh:
                for ln in remaining:
                    fh.write(ln + "\n")
        logger.info("Pending deals: %d/%d still pending", len(remaining), len(lines))
    else:
        logger.info("Pending deals: all %d entries processed", len(lines))


def _parse_dt(raw: str, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(raw) if raw else default
    except ValueError:
        return default


def _retry_loop(mt5_lock, mt5_account_id: str, worker_name: str) -> None:
    while True:
        time.sleep(RETRY_INTERVAL)
        try:
            process_queue(mt5_lock, mt5_account_id, worker_name)
        except Exception as exc:
            logger.error("Pending deals retry sweep error: %s", exc)


def start_pending_retry_worker(mt5_lock, mt5_account_id: str, worker_name: str) -> None:
    t = threading.Thread(
        target=_retry_loop,
        args=(mt5_lock, mt5_account_id, worker_name),
        daemon=True,
        name="journal-pending-retry",
    )
    t.start()
    logger.info("Journal pending deals retry worker started (interval=%ds)", RETRY_INTERVAL)
