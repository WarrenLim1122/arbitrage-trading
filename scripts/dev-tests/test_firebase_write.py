"""
Live Firebase connectivity test — writes a real document to Firestore and reads it back.

Run from C:\arbitrage on VPS #2:
    uv run python scripts/test_firebase_write.py

Requires .env with all FIREBASE_* vars set (FIREBASE_JOURNAL_DRY_RUN is ignored here).
On success: prints "FIRESTORE WRITE OK" and the trade will appear at warrenlimzf.com/journal.
On failure: prints the exact error so we know what to fix.
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

PROJECT_ID    = os.getenv("FIREBASE_PROJECT_ID", "")
SA_PATH       = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "")
USER_ID       = os.getenv("FIREBASE_JOURNAL_USER_ID", "")
COLLECTION    = os.getenv("FIREBASE_JOURNAL_COLLECTION", "trades")
DATABASE_ID   = os.getenv("FIREBASE_DATABASE_ID", "(default)")

print("=" * 60)
print("Firebase Connectivity Test")
print("=" * 60)
print(f"  Project ID:       {PROJECT_ID}")
print(f"  Service account:  {SA_PATH}")
print(f"  User ID:          {USER_ID}")
print(f"  Collection:       {COLLECTION}")
print(f"  Database ID:      {DATABASE_ID}")
print(f"  SA file exists:   {Path(SA_PATH).exists() if SA_PATH else False}")
print("=" * 60)

if not PROJECT_ID:
    print("FAIL: FIREBASE_PROJECT_ID is not set in .env")
    sys.exit(1)
if not SA_PATH:
    print("FAIL: FIREBASE_SERVICE_ACCOUNT_PATH is not set in .env")
    sys.exit(1)
if not Path(SA_PATH).exists():
    print(f"FAIL: Service account file not found at: {SA_PATH}")
    sys.exit(1)
if not USER_ID:
    print("FAIL: FIREBASE_JOURNAL_USER_ID is not set in .env")
    sys.exit(1)

print("\nStep 1 — Initialising Firestore client...")
try:
    from google.oauth2 import service_account
    from google.cloud import firestore

    creds = service_account.Credentials.from_service_account_file(SA_PATH)
    db = firestore.Client(
        project=PROJECT_ID,
        credentials=creds,
        database=DATABASE_ID,
    )
    print(f"  Firestore client OK (database={DATABASE_ID})")
except Exception as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

doc_id  = "connection_test_delete_me"
now_iso = datetime.now(timezone.utc).isoformat()
ref     = db.collection("users").document(USER_ID).collection(COLLECTION).document(doc_id)

print(f"\nStep 2 — Writing test document...")
print(f"  Path: users/{USER_ID}/{COLLECTION}/{doc_id}")
try:
    ref.set({
        "id":          doc_id,
        "userId":      USER_ID,
        "source":      "connection_test",
        "symbol":      "NZDUSD",
        "pair":        "NZDUSD",
        "direction":   "LONG",
        "position":    "Long",
        "outcome":     "WIN",
        "netPnl":      129.72,
        "pnlAmount":   129.72,
        "grossPnl":    129.72,
        "commission":  0.0,
        "swap":        0.0,
        "volume":      1.41,
        "entryPrice":  0.59770,
        "closePrice":  0.59868,
        "stopLoss":    0.59420,
        "takeProfit":  0.59868,
        "closeReason": "TP",
        "accountType": "demo",
        "broker":      "MetaQuotes Demo",
        "marketType":  "Forex",
        "ticket":      8509290565,
        "mt5AccountId": "106497299",
        "magicNumber": 20250002,
        "openTime":    "2026-05-06T10:45:03+00:00",
        "closeTime":   "2026-05-06T10:53:16+00:00",
        "date":        "2026-05-06T10:53:16+00:00",
        "tags":        ["bot", "arbitrage", "personal"],
        "notes":       "Connection test — you should see this on warrenlimzf.com/journal",
        "createdAt":   now_iso,
        "updatedAt":   now_iso,
        "importedAt":  now_iso,
        "botName":     "HedgeHog Bot",
        "strategyName": "Arbitrage Trading",
    }, merge=True)
    print("  Write OK")
except Exception as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

print("\nStep 3 — Reading it back to verify...")
try:
    snap = ref.get()
    if snap.exists:
        data = snap.to_dict()
        print(f"  Read OK — netPnl={data.get('netPnl')}  outcome={data.get('outcome')}")
    else:
        print("  FAIL: document was written but could not be read back")
        sys.exit(1)
except Exception as exc:
    print(f"  FAIL: {exc}")
    sys.exit(1)

print()
print("=" * 60)
print("FIRESTORE WRITE OK")
print(f"Check warrenlimzf.com/journal — you should see a NZDUSD WIN trade.")
print(f"Document path: users/{USER_ID}/{COLLECTION}/{doc_id}")
print("=" * 60)
