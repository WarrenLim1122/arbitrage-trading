"""Upload PNG screenshots to Firebase Storage and return public URL."""

import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCREENSHOT_STORAGE        = os.getenv("SCREENSHOT_STORAGE",      "firebase")
SCREENSHOT_DRY_RUN        = os.getenv("SCREENSHOT_DRY_RUN",      "false").lower() == "true"
FIREBASE_STORAGE_BUCKET   = os.getenv("FIREBASE_STORAGE_BUCKET", "")
FIREBASE_SERVICE_ACCOUNT_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "")

_storage_client = None


def _get_storage_client():
    global _storage_client
    if _storage_client is not None:
        return _storage_client
    if not FIREBASE_SERVICE_ACCOUNT_PATH:
        logger.error("FIREBASE_SERVICE_ACCOUNT_PATH not set — cannot init storage client")
        return None
    try:
        from google.oauth2 import service_account
        from google.cloud import storage

        creds = service_account.Credentials.from_service_account_file(
            FIREBASE_SERVICE_ACCOUNT_PATH
        )
        _storage_client = storage.Client(credentials=creds)
        logger.info("Google Cloud Storage client initialised")
        return _storage_client
    except Exception as exc:
        logger.error("Storage client init failed: %s", exc)
        return None


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

    client = _get_storage_client()
    if client is None:
        return None

    try:
        bucket = client.bucket(FIREBASE_STORAGE_BUCKET)
        blob   = bucket.blob(blob_path)
        blob.cache_control = "no-cache, no-store, must-revalidate"
        blob.upload_from_filename(str(local_path), content_type="image/png")
        blob.make_public()
        # Append timestamp so the URL in Firestore changes on every upload,
        # forcing the journal website to fetch the new image regardless of CDN cache.
        url = f"{blob.public_url}?t={int(time.time())}"
        logger.info("Screenshot uploaded → %s", url)
        return url
    except Exception as exc:
        logger.error("Screenshot upload failed: %s", exc)
        return None
