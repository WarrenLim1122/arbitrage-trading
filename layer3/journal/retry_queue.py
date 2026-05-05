"""
Persist failed journal payloads to disk and retry them on a background thread.

Retry queue file: journal_retry_queue.jsonl (project root — .gitignored)
Each line is one JSON payload.  On retry the line is removed on success.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RETRY_QUEUE_PATH = Path(__file__).parent.parent.parent / "journal_retry_queue.jsonl"
RETRY_INTERVAL   = 300   # 5 minutes between retry sweeps

_lock = threading.Lock()


def enqueue(payload: dict) -> None:
    """Append a failed journal payload to the on-disk retry queue."""
    try:
        with _lock:
            with RETRY_QUEUE_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")
        logger.info("Journal: payload queued for retry (ticket=%s)", payload.get("ticket"))
    except Exception as exc:
        logger.error("Failed to write retry queue: %s", exc)


def _retry_loop() -> None:
    from .firebase_journal import write_trade  # avoid circular at module level

    while True:
        time.sleep(RETRY_INTERVAL)
        if not RETRY_QUEUE_PATH.exists():
            continue

        try:
            with _lock:
                raw = RETRY_QUEUE_PATH.read_text(encoding="utf-8").splitlines()
                RETRY_QUEUE_PATH.write_text("", encoding="utf-8")   # clear atomically
        except Exception as exc:
            logger.error("Retry queue read error: %s", exc)
            continue

        lines     = [ln.strip() for ln in raw if ln.strip()]
        remaining = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not write_trade(payload):
                remaining.append(line)

        if remaining:
            with _lock:
                with RETRY_QUEUE_PATH.open("a", encoding="utf-8") as fh:
                    for ln in remaining:
                        fh.write(ln + "\n")
            logger.warning(
                "Journal retry: %d/%d payloads still failing", len(remaining), len(lines)
            )
        elif lines:
            logger.info("Journal retry: all %d queued payloads written successfully", len(lines))


def start_retry_worker() -> None:
    t = threading.Thread(target=_retry_loop, daemon=True, name="journal-retry")
    t.start()
    logger.info("Journal retry worker started (interval=%ds)", RETRY_INTERVAL)
