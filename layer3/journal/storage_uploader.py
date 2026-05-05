"""Upload PNG screenshots to Firebase Storage and return public URL."""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCREENSHOT_STORAGE      = os.getenv("SCREENSHOT_STORAGE",     "firebase")
SCREENSHOT_DRY_RUN      = os.getenv("SCREENSHOT_DRY_RUN",     "false").lower() == "true"
FIREBASE_STORAGE_BUCKET = os.getenv("FIREBASE_STORAGE_BUCKET", "")


def upload_screenshot(
    local_path: Path,
    account_type: str,
    mt5_account_id: str,
    ticket: int,
) -> Optional[str]:
    """
    Upload screenshot to Firebase Storage.

    Storage path: trade-screenshots/{accountType}/{mt5AccountId}/{ticket}/outcome.png

    Returns public URL on success, None on failure.
    In dry-run mode returns a placeholder URL and skips upload.
    """
    blob_path = (
        f"trade-screenshots/{account_type}/{mt5_account_id}/{ticket}/outcome.png"
    )

    if SCREENSHOT_DRY_RUN:
        logger.info("[DRY RUN] Would upload %s → gs://%s/%s",
                    local_path, FIREBASE_STORAGE_BUCKET or "<bucket>", blob_path)
        return f"dry_run://storage/{blob_path}"

    if SCREENSHOT_STORAGE != "firebase":
        logger.warning("Unsupported SCREENSHOT_STORAGE=%s", SCREENSHOT_STORAGE)
        return None

    if not FIREBASE_STORAGE_BUCKET:
        logger.error("FIREBASE_STORAGE_BUCKET not set — screenshot upload skipped")
        return None

    try:
        from firebase_admin import storage as fb_storage  # firebase_admin already init'd

        bucket = fb_storage.bucket(FIREBASE_STORAGE_BUCKET)
        blob   = bucket.blob(blob_path)
        blob.upload_from_filename(str(local_path), content_type="image/png")
        blob.make_public()
        url = blob.public_url
        logger.info("Screenshot uploaded → %s", url)
        return url
    except Exception as exc:
        logger.error("Screenshot upload failed: %s", exc)
        return None
