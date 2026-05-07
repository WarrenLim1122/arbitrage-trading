"""
Persistent retry queue for journal entries where MT5 deal history wasn't available.

When the inline 7-attempt retry in journaling_worker exhausts without finding the exit
deal, the position snapshot is saved here. A background thread retries every
RETRY_INTERVAL seconds, calling handle_closed_position with skip_retry=True (one attempt,
no sleep). Entries older than MAX_AGE_HOURS are dropped.

Queue file: journal_pending_deals.jsonl (project root — .gitignored)
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PENDING_QUEUE_PATH = Path(__file__).parent.parent.parent / "journal_pending_deals.jsonl"
RETRY_INTERVAL = 600     # 10 minutes between sweeps
MAX_AGE_HOURS  = 24

_lock = threading.Lock()


def process_queue(mt5_lock, mt5_account_id: str, worker_name: str) -> None:
    """Attempt to journal all pending deals. Keeps failures for next retry."""
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

        ticket = entry.get("ticket", 0)

        # Drop entries older than MAX_AGE_HOURS
        queued_raw = entry.get("queued_at", "")
        if queued_raw:
            try:
                queued_at = datetime.fromisoformat(queued_raw)
                age_h = (datetime.now(timezone.utc) - queued_at).total_seconds() / 3600
                if age_h > MAX_AGE_HOURS:
                    logger.warning(
                        "Pending deal too old (%.1fh > %dh) — dropping ticket=%d",
                        age_h, MAX_AGE_HOURS, ticket,
                    )
                    continue
            except ValueError:
                pass

        # Reconstruct snapshot, converting open_time back to a datetime
        snapshot = dict(entry.get("snapshot", {}))
        if "open_time" in snapshot and isinstance(snapshot["open_time"], str):
            try:
                snapshot["open_time"] = datetime.fromisoformat(snapshot["open_time"])
            except ValueError:
                snapshot["open_time"] = datetime.now(timezone.utc)

        result = handle_closed_position(
            mt5_lock=mt5_lock,
            mt5_account_id=entry.get("mt5_account_id", mt5_account_id),
            worker_name=entry.get("worker_name", worker_name),
            position_ticket=ticket,
            pos_snapshot=snapshot,
            skip_retry=True,
        )

        if result is None:
            # Deal still not available — re-add with original queued_at preserved
            remaining.append(line)
        else:
            logger.info("Pending deal journaled successfully: ticket=%d", ticket)

    if remaining:
        with _lock:
            with _PENDING_QUEUE_PATH.open("a", encoding="utf-8") as fh:
                for ln in remaining:
                    fh.write(ln + "\n")
        logger.info("Pending deals: %d/%d still pending", len(remaining), len(lines))
    else:
        logger.info("Pending deals: all %d entries processed", len(lines))


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
