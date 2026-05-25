"""Diagnostic: determine how to get the MT5 Python library connected to the
.env account on THIS VPS.

Known so far:
  * Switching accounts at runtime kills the IPC (-10005).
  * Library self-launch boots into a sticky MetaQuotes demo (not our target).
  * /config subprocess launch reaches the target, but it's unknown whether the
    library can attach to a terminal it didn't launch.

This runs two tests and writes everything to config/autologin_diag.txt so a
frozen ("Select") console can't lose the result.

Run on the VPS (don't click inside the window), then show me the file:
    cd C:\\arbitrage
    uv run --extra layer3 python layer3/autologin_check.py
    type config\\autologin_diag.txt
"""
from __future__ import annotations

import glob
import os
import subprocess
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

base = Path(__file__).resolve().parent.parent / "config"
out = base / "autologin_diag.txt"
ini = base / "mt5_autologin.ini"
_lines: list[str] = []


def log(s: str = "") -> None:
    print(s)
    _lines.append(s)
    out.write_text("\n".join(_lines), encoding="utf-8")


def kill(force: bool = True) -> None:
    args = ["taskkill"] + (["/F"] if force else []) + ["/IM", "terminal64.exe"]
    subprocess.run(args, capture_output=True, text=True)


def running() -> bool:
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
                       capture_output=True, text=True)
    return "terminal64.exe" in r.stdout


def acct_line(tag: str, ok) -> None:
    ai = mt5.account_info()
    ti = mt5.terminal_info()
    log(f"{tag}: ok={ok} err={mt5.last_error()} "
        f"account={ai.login if ai else None} connected={ti.connected if ti else None}")
    if ai and ai.login == LOGIN:
        log(f"    --> ON TARGET ({LOGIN})")


log(f"target   = {LOGIN} on {SERVER}")
log(f"terminal = {term}")
ini.write_text(f"[Common]\nLogin={LOGIN}\nPassword={PASSWORD}\nServer={SERVER}\n",
               encoding="utf-8")

kill()
time.sleep(2)

# ── TEST 1: can the library ATTACH to a /config-launched terminal? ───────────
log("\n=== TEST 1: attach to a /config-launched terminal (already on target) ===")
subprocess.Popen([term, f"/config:{ini}"])
log("launched with /config; waiting 40s to settle on target...")
time.sleep(40)
ok = mt5.initialize()          # no path -> pure attach to the running terminal
acct_line("initialize() no-path", ok)
mt5.shutdown()
time.sleep(3)
ok = mt5.initialize(term)      # path -> attach to running instance
acct_line("initialize(path)   ", ok)
mt5.shutdown()

# ── TEST 2: does /config persist as the default after a CLEAN close? ─────────
log("\n=== TEST 2: does /config persist as default after a clean close ===")
kill(force=False)              # graceful WM_CLOSE so it can save
exited = False
for _ in range(30):
    if not running():
        exited = True
        break
    time.sleep(2)
log(f"graceful close: clean_exit={exited}")
kill()
time.sleep(3)
ok = mt5.initialize(term)      # library self-launch -> auto-login to its default
acct_line("library self-launch", ok)
mt5.shutdown()
kill()

log("\n=== READS ===")
log("TEST 1 ON TARGET  -> attach works -> simplest fix (launch /config, attach).")
log("TEST 2 ON TARGET  -> set-default sticks -> set via /config, then self-launch.")
log("Neither ON TARGET -> need a dedicated data dir / persistent config; tell Claude.")
