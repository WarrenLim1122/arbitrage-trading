"""
Write completed trades to Firestore via Google Cloud Firestore SDK.

Schema follows FIRESTORE_TRADE_SCHEMA.md and BOT_JOURNALING_API.md exactly.
Firestore path: users/{userId}/trades/{tradeId}
Document ID:    {accountType}_{mt5AccountId}_{ticket}
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

FIREBASE_JOURNAL_ENABLED      = os.getenv("FIREBASE_JOURNAL_ENABLED", "false").lower() == "true"
FIREBASE_JOURNAL_DRY_RUN      = os.getenv("FIREBASE_JOURNAL_DRY_RUN", "true").lower() == "true"
FIREBASE_PROJECT_ID           = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_SERVICE_ACCOUNT_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "")
FIREBASE_JOURNAL_USER_ID      = os.getenv("FIREBASE_JOURNAL_USER_ID", "")
FIREBASE_JOURNAL_COLLECTION   = os.getenv("FIREBASE_JOURNAL_COLLECTION", "trades")
# Named Firestore database — leave blank or "(default)" for projects with only one database.
# Find in Firebase Console → Firestore → database selector dropdown.
FIREBASE_DATABASE_ID          = os.getenv("FIREBASE_DATABASE_ID", "(default)")

_db_client = None


def _get_db():
    """Return a cached Firestore client, initialising it on first call."""
    global _db_client
    if _db_client is not None:
        return _db_client

    if not FIREBASE_PROJECT_ID or not FIREBASE_SERVICE_ACCOUNT_PATH:
        logger.error(
            "Firestore not configured — set FIREBASE_PROJECT_ID and "
            "FIREBASE_SERVICE_ACCOUNT_PATH in .env"
        )
        return None

    try:
        from google.oauth2 import service_account
        from google.cloud import firestore

        creds = service_account.Credentials.from_service_account_file(
            FIREBASE_SERVICE_ACCOUNT_PATH
        )
        _db_client = firestore.Client(
            project=FIREBASE_PROJECT_ID,
            credentials=creds,
            database=FIREBASE_DATABASE_ID,
        )
        logger.info(
            "Firestore client initialised (project=%s  database=%s)",
            FIREBASE_PROJECT_ID, FIREBASE_DATABASE_ID,
        )
        return _db_client
    except Exception as exc:
        logger.error("Firestore client init failed: %s", exc)
        return None


def build_document_id(account_type: str, mt5_account_id: str, ticket: int) -> str:
    """Deterministic document ID — prevents duplicate journal entries."""
    return f"{account_type}_{mt5_account_id}_{ticket}"


def derive_market_type(symbol: str) -> str:
    sym = symbol.upper().replace(".", "").replace("-", "").replace("_", "")
    if sym in {"XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"}:
        return "Metals"
    if sym in {"BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "BNBUSD", "SOLUSD"}:
        return "Crypto"
    if sym in {"NAS100", "US500", "US30", "DAX40", "UK100", "GER40", "SPX500"}:
        return "Indices"
    currencies = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
                  "SGD", "HKD", "SEK", "NOK", "DKK", "MXN", "TRY"}
    if len(sym) == 6 and sym[:3] in currencies and sym[3:] in currencies:
        return "Forex"
    return "Forex"


def write_trade(payload: dict) -> bool:
    """
    Write or update a trade document in Firestore (upsert semantics).
    Returns True on success or dry-run, False on failure.
    """
    if not FIREBASE_JOURNAL_ENABLED:
        logger.info("FIREBASE_JOURNAL_ENABLED=false — journal write skipped")
        return True

    if not FIREBASE_JOURNAL_USER_ID:
        logger.error("FIREBASE_JOURNAL_USER_ID not set — cannot journal trade")
        return False

    doc_id = payload.get("id") or build_document_id(
        payload["accountType"], str(payload["mt5AccountId"]), payload["ticket"]
    )
    payload["id"] = doc_id

    if FIREBASE_JOURNAL_DRY_RUN:
        logger.info(
            "[DRY RUN] Firestore write skipped.\n"
            "  Path:    users/%s/%s/%s\n"
            "  Payload: %s",
            FIREBASE_JOURNAL_USER_ID,
            FIREBASE_JOURNAL_COLLECTION,
            doc_id,
            json.dumps(payload, indent=2, default=str),
        )
        return True

    db = _get_db()
    if db is None:
        return False

    try:
        ref = (
            db.collection("users")
              .document(FIREBASE_JOURNAL_USER_ID)
              .collection(FIREBASE_JOURNAL_COLLECTION)
              .document(doc_id)
        )
        ref.set(payload, merge=True)
        logger.info(
            "Firestore write OK: users/%s/%s/%s",
            FIREBASE_JOURNAL_USER_ID, FIREBASE_JOURNAL_COLLECTION, doc_id,
        )
        return True
    except Exception as exc:
        logger.error("Firestore write failed: %s", exc)
        return False
