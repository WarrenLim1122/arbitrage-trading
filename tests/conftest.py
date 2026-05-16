"""Shared pytest setup.

Strategy modules are pure and need none of this. It exists only so tests that
import layer2.state (which reads TELEGRAM_* env vars at import time) do not
crash during collection.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
