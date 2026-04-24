"""
Layer 3 — Prop firm worker (VPS #2, FundingPips MT5)

Run:  python layer3/worker_prop.py

Required env vars (set in .env on VPS #2):
  MT5_LOGIN      — FundingPips account number
  MT5_PASSWORD   — FundingPips account password
  MT5_SERVER     — FundingPips MT5 server name
"""

import os

os.environ.setdefault("WORKER_NAME", "prop")
os.environ.setdefault("MT5_MAGIC",   "20250001")

from layer3._worker_core import main  # noqa: E402

if __name__ == "__main__":
    main()
