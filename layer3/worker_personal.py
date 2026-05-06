"""
Layer 3 — Personal account worker (VPS #2, Fusion Markets MT5)

Run:  python layer3/worker_personal.py

Required env vars (set in .env on VPS #2):
  MT5_LOGIN      — Fusion Markets account number
  MT5_PASSWORD   — Fusion Markets account password
  MT5_SERVER     — Fusion Markets MT5 server name
"""

import os

os.environ.setdefault("WORKER_NAME", "personal")
os.environ.setdefault("MT5_MAGIC",   "20250002")

from layer3._worker_core import main  # noqa: E402

if __name__ == "__main__":
    main()
