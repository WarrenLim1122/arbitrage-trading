"""One-off validation for the dynamic auto-login design (two-phase).

Problem recap:
  * Switching accounts at runtime kills the Python<->terminal IPC (-10005).
  * The MetaTrader5 library can only attach to a terminal IT launched itself,
    not one launched by a separate process. So we can't just launch with
    /config and attach.

Two-phase solution (driven entirely by .env):
  Phase 1 — launch terminal64.exe WITH a /config startup .ini built from
            MT5_LOGIN/PASSWORD/SERVER, so MT5 logs into the target account and
            saves it as the terminal's default. Then close it (gracefully, so
            the default persists).
  Phase 2 — let mt5.initialize(path) launch its OWN terminal. It auto-logs into
            the now-saved default (= target account). The library owns this
            terminal, so the IPC works. No account switch ever happens.

Run on the VPS (do NOT click inside the window while it runs — that freezes it):
    cd C:\\arbitrage
    uv run --extra layer3 python layer3/autologin_check.py
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import MetaTrader5 as mt5

load_dotenv()

LOGIN = int(os.environ["MT5_LOGIN"])
PASSWORD = os.environ["MT5_PASSWORD"]
SERVER = os.environ["MT5_SERVER"]

term = os.getenv("MT5_TERMINAL_PATH")
if not term or not os.path.exists(term):
    matches = glob.glob(r"C:\Program Files\MetaTrader*\terminal64.exe")
    if matches:
        term = matches[0]
print(f"terminal path = {term}")
if not term or not os.path.exists(term):
    sys.exit("ERROR: could not find terminal64.exe")

ini_path = Path(__file__).resolve().parent.parent / "config" / "mt5_autologin.ini"
ini_path.write_text(
    "[Common]\n"
    f"Login={LOGIN}\n"
    f"Password={PASSWORD}\n"
    f"Server={SERVER}\n",
    encoding="utf-8",
)
print(f"target        = login {LOGIN} on {SERVER}")


def kill_terminals(graceful: bool) -> None:
    args = ["taskkill", "/IM", "terminal64.exe"] if graceful else ["taskkill", "/F", "/IM", "terminal64.exe"]
    subprocess.run(args, capture_output=True, text=True)


# ── Phase 1: set the target account as the terminal's saved default ──────────
print("\n[Phase 1] launching with /config to set the default account ...")
kill_terminals(graceful=False)
time.sleep(2)
subprocess.Popen([term, f"/config:{ini_path}"])
print("[Phase 1] waiting 35s for login + config persist ...")
time.sleep(35)
print("[Phase 1] closing the config-launched terminal (graceful, so it saves) ...")
kill_terminals(graceful=True)
time.sleep(8)
kill_terminals(graceful=False)
time.sleep(3)

# ── Phase 2: library launches its OWN terminal -> auto-login to saved default ─
print("\n[Phase 2] library launching its own terminal (auto-login to default) ...")
deadline = time.time() + 120
ok = False
ai = ti = None
while time.time() < deadline:
    if mt5.initialize(term):
        ti = mt5.terminal_info()
        ai = mt5.account_info()
        acct = ai.login if ai else None
        conn = ti.connected if ti else None
        print(f"  ... init ok: account={acct} connected={conn}")
        if ai and ai.login == LOGIN and ti and ti.connected:
            ok = True
            break
    else:
        print(f"  ... init not ready ({mt5.last_error()})")
    time.sleep(5)

print("-" * 50)
print(f"last_error = {mt5.last_error()}")
if ai:
    print(f"account    = {ai.login}")
    print(f"server     = {ai.server}")
    print(f"balance    = {ai.balance}")
if ti:
    print(f"connected     = {ti.connected}")
    print(f"trade_allowed = {ti.trade_allowed}")
print("=" * 50)
print("RESULT:", "MATCH - auto-login works" if ok else "NO MATCH - needs adjustment")
mt5.shutdown()
