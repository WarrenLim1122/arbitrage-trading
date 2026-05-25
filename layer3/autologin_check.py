"""One-off validation for the dynamic auto-login design.

Proves we can connect to the account specified in `.env` WITHOUT a runtime
account switch (which kills the Python<->terminal IPC pipe, see -10005 timeout).

Mechanism:
  1. Read MT5_LOGIN / MT5_PASSWORD / MT5_SERVER from `.env`.
  2. Write a tiny MT5 startup .ini ([Common] Login/Password/Server).
  3. Kill any running terminal, then launch terminal64.exe WITH that .ini so it
     auto-logs straight into the target account (no demo first -> no switch).
  4. Attach with mt5.initialize() (no creds) and verify we landed on the right
     account with a live connection.

Run on the VPS:
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

# Resolve terminal path: env override, else auto-discover. The wildcard avoids
# the 'MetaTrader 5' space getting mangled on paste.
term = os.getenv("MT5_TERMINAL_PATH")
if not term or not os.path.exists(term):
    matches = glob.glob(r"C:\Program Files\MetaTrader*\terminal64.exe")
    if matches:
        term = matches[0]
print(f"terminal path = {term}")
if not term or not os.path.exists(term):
    sys.exit("ERROR: could not find terminal64.exe")

# Write the startup config next to the project config dir.
ini_path = Path(__file__).resolve().parent.parent / "config" / "mt5_autologin.ini"
ini_path.write_text(
    "[Common]\n"
    f"Login={LOGIN}\n"
    f"Password={PASSWORD}\n"
    f"Server={SERVER}\n",
    encoding="utf-8",
)
print(f"wrote ini  = {ini_path}")
print(f"target     = login {LOGIN} on {SERVER}")

# Kill any running terminal so our ini-launch is the only instance.
subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"], capture_output=True, text=True)
time.sleep(2)

# Launch the terminal ourselves WITH the startup config -> direct auto-login.
print("launching terminal with /config ...")
subprocess.Popen([term, f"/config:{ini_path}"])

# Poll: attach, then wait until it's connected on the right account.
deadline = time.time() + 180
ok = False
ai = ti = None
while time.time() < deadline:
    time.sleep(5)
    attached = mt5.initialize() or mt5.initialize(term)
    if not attached:
        print(f"  ... not attached yet ({mt5.last_error()})")
        continue
    ti = mt5.terminal_info()
    ai = mt5.account_info()
    acct = ai.login if ai else None
    conn = ti.connected if ti else None
    print(f"  ... attached: account={acct} connected={conn}")
    if ai and ai.login == LOGIN and ti and ti.connected:
        ok = True
        break

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
