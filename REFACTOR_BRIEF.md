# logic_core.py Refactor Brief
**For next Claude session only — delete this file after the refactor is done.**

## Goal
Split `layer2/logic_core.py` (~2,900 lines) into 4 focused files.
**No logic changes. No variable renames. No deletions. Pure file reorganisation.**

---

## Target Structure

```
layer2/
  state.py            — all globals, locks, path constants, config loading
  zmq_helpers.py      — ZMQ communication + dispatch helpers
  telegram_handlers.py — all Telegram /command handlers
  logic_core.py       — FastAPI app, signal endpoint, monitoring loops, bot startup
```

Import chain (no circular imports):
```
state.py        ← no internal imports
zmq_helpers.py  ← imports state
telegram_handlers.py ← imports state, zmq_helpers
logic_core.py   ← imports state, zmq_helpers, telegram_handlers
```

---

## Step-by-Step Instructions

### STEP 0 — Read the whole current logic_core.py first
Do NOT start editing until you have read the entire file. Use Read with offset/limit
to cover all ~2,900 lines. Map every function to its destination file before touching anything.

### STEP 1 — Create `layer2/state.py`
Move these blocks verbatim (copy, then delete from logic_core.py later):

**Imports needed in state.py:**
```python
import json, os, threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
```

**Content to move (exact line ranges in current logic_core.py):**
- All `Path` constants (ROOT, PHASE_CONFIG_PATH, RISK_PARAMS_PATH, PROPFIRM_CONFIG_PATH,
  CONSISTENCY_LOG_PATH, SYMBOL_MAP_PATH, _ALLOWED_PAIRS_PATH)
- All env var loading (BOT_TOKEN, CHAT_ID)
- All config loading blocks (_risk, _SYMBOL_MAP, _pair_config, ALLOWED_PAIRS, _TICKER_CURRENCIES,
  PROP_RISK_PCT, PHASE_MULT, ZMQ_PUSH_PROP, ZMQ_PUSH_PERS, ZMQ_REQ_PROP, ZMQ_REQ_PERS,
  EQUITY_TIMEOUT, _NEWS_AWARENESS_WINDOW, _NEWS_TRADING_BAN_WINDOW)
- All global variables and their locks:
  _phase_state, _state_lock, _propfirm, _pf_lock, _consistency_log, _consistency_lock,
  _news_suppressed_pairs, _news_suppressed_lock, _manual_suppressed_pairs, _manual_suppress_lock,
  _news_closed_events, _news_events_lock, _mismatch_first_seen,
  _prop_down, _pers_down, _prop_fail_count, _pers_fail_count, _prop_algo_disabled, _pers_algo_disabled,
  _WORKER_DOWN_THRESHOLD, _last_curfew_close_date, _zmq_ctx
- Load/save functions: _load_phase, _save_phase, _load_propfirm, _save_propfirm,
  _load_consistency_log, _save_consistency_log, _reset_consistency_log,
  _record_day_profit, _build_consistency_table
- Pure utility functions: _sgt_now, _propfirm_day, _is_sgt_curfew, _propfirm_day,
  _invert, _apply_buffers, _pnl_bar, _p2_display, _p2_settings_block

**SGT constant:**
```python
SGT = ZoneInfo("Asia/Singapore")
```

### STEP 2 — Create `layer2/zmq_helpers.py`
Move these functions verbatim:

```python
from layer2.state import (
    _zmq_ctx, EQUITY_TIMEOUT, ZMQ_PUSH_PROP, ZMQ_PUSH_PERS,
    _phase_state, _state_lock,  # for _dispatch_force_close
    # ... import everything these functions reference from state
)
import zmq, json, asyncio, logging
```

**Functions to move:**
- `_query_equity` (line ~288)
- `_push_ticket` (line ~321)
- `_query_positions` (line ~1889)
- `_snapshot_positions_str` (line ~1903)
- `_dispatch_force_close` (line ~402)
- `_dispatch_close_ticker` (line ~429)
- `_dispatch_news_suppress` (line ~440)
- `_dispatch_news_clear` (line ~455)
- `_close_ticker_on_worker` (line ~468)
- `_alert_sync` (line ~374)
- `_telegram_alert` (line ~387)
- `_lock_baseline_from_live` (line ~693)
- `_dispatch_parameters` (line ~717)
- `_update_day_start` (line ~685)

### STEP 3 — Create `layer2/telegram_handlers.py`
Move ALL `_cmd_*`, `_wiz_*`, `_p2_*`, `_emergency_*`, `_closepair_*` functions and the
`_run_bot()` function. Also move `_auth`.

**Imports at top of telegram_handlers.py:**
```python
import asyncio, logging
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ConversationHandler, MessageHandler,
    filters, ContextTypes,
)
from layer2.state import (
    BOT_TOKEN, CHAT_ID, _phase_state, _state_lock, _propfirm, _pf_lock,
    _consistency_log, _consistency_lock, _news_suppressed_pairs, _news_suppressed_lock,
    _manual_suppressed_pairs, _manual_suppress_lock, _news_closed_events, _news_events_lock,
    ALLOWED_PAIRS, _TICKER_CURRENCIES, _NEWS_TRADING_BAN_WINDOW,
    _save_phase, _save_propfirm, _save_consistency_log, _reset_consistency_log,
    _record_day_profit, _build_consistency_table, _p2_display, _p2_settings_block,
    _is_sgt_curfew, _sgt_now, _propfirm_day,
)
from layer2.zmq_helpers import (
    _query_equity, _query_positions, _snapshot_positions_str,
    _dispatch_force_close, _dispatch_close_ticker, _dispatch_news_suppress,
    _dispatch_news_clear, _close_ticker_on_worker, _telegram_alert, _alert_sync,
    _lock_baseline_from_live, _dispatch_parameters,
    ZMQ_REQ_PROP, ZMQ_REQ_PERS, ZMQ_PUSH_PROP, ZMQ_PUSH_PERS,
)
```

**Functions to move (lines ~1044–2509):**
- `_auth`
- All `_cmd_*` functions
- All `_wiz_*` functions (changepropfirm wizard)
- All `_p2_*` functions (phase2 wizard)
- `_emergency_execute`, `_emergency_abort`
- `_closepair_execute`, `_closepair_abort`
- `_run_bot()` — the function that builds the Telegram Application and registers all handlers

### STEP 4 — Clean up `layer2/logic_core.py`
What remains in logic_core.py after moving everything:

**Imports:**
```python
import asyncio, json, logging, threading, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx, zmq
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from layer2.state import *   # or explicit imports of everything used
from layer2.zmq_helpers import *
from layer2 import telegram_handlers
```

**What stays in logic_core.py:**
- Logging setup (the logging.basicConfig block)
- `_handle_mismatch` and `_run_mismatch_check` (lines ~477–558)
- `_run_news_preclose_check` + `_news_preclose_loop` (lines ~559–684)
- `_equity_monitor_loop` + `_run_equity_check` (lines ~750–1043)
- FastAPI lifespan + app = FastAPI() + startup tasks
- `SignalPayload` Pydantic model (line ~2511)
- `_query_positions_with_retry` (line ~2544)
- `_verify_and_notify` (line ~2558)
- `receive_signal` endpoint (@app.post("/signal"), line ~2634)
- `health` endpoint (line ~2893)
- `news_status` endpoint (line ~2911)
- The `if __name__ == "__main__"` block or equivalent entrypoint

---

## Critical Rules — Do NOT Violate

1. **Zero logic changes.** Every function body must be byte-for-byte identical after the move.
   The only things that change are: file location, and import statements at the top of each file.

2. **No renames.** Every variable, function, and class keeps its exact current name.

3. **Read before write.** Read each section fully before moving it. Use grep to verify
   every symbol is accounted for — nothing left behind, nothing duplicated.

4. **One file at a time.** Complete state.py → verify Python parses it → zmq_helpers.py →
   verify → telegram_handlers.py → verify → clean up logic_core.py → verify.
   Run `python3 -c "import layer2.state"` etc. after each step.

5. **Do NOT change the uvicorn entry point.** `layer2/logic_core.py` must still expose `app`.
   The systemd service on VPS #1 runs: `uvicorn layer2.logic_core:app`
   If you rename or restructure the app object, the service will fail to start.

6. **Test the import chain before deploying:**
   ```bash
   cd /root/arbitrage-trading
   python3 -c "from layer2 import logic_core; print('OK')"
   ```
   This must print OK with no errors before restarting Layer 2.

7. **`_zmq_ctx` must be initialised exactly once.** Currently it's a module-level global.
   It must stay as a module-level global in `state.py` — do not move it inside a function.

---

## Verification After Refactor

**On local machine (before pushing):**
```bash
python3 -c "from layer2 import state; print('state OK')"
python3 -c "from layer2 import zmq_helpers; print('zmq_helpers OK')"
python3 -c "from layer2 import telegram_handlers; print('telegram_handlers OK')"
python3 -c "from layer2 import logic_core; print('logic_core OK')"
```
All four must print OK.

**On VPS #1 (after git pull):**
```bash
sudo systemctl restart layer2
sleep 3
systemctl status layer2   # must show "active (running)"
journalctl -u layer2 -n 30 --no-pager   # must show no ImportError or AttributeError
```

**Send a Telegram command:**
Send `/status` to the bot. If it replies, the Telegram handlers are wired correctly.

**Send a test signal (if comfortable):**
Use `/resume` then trigger a TradingView alert manually. Verify "Trade Confirmed" appears.

---

## Layer 1 and Layer 3 — No Changes Needed

- **Layer 1** (`layer1/main.py`) communicates with Layer 2 only via HTTP POST to
  `http://127.0.0.1:8001/signal`. As long as `app` stays in `logic_core.py`, Layer 1 is unaffected.
- **Layer 3** (`layer3/_worker_core.py`) communicates via ZMQ only. Completely independent.
  No changes to Layer 3 are needed for this refactor.

---

## If Something Breaks

Do NOT try to fix it by guessing. Roll back immediately:
```bash
ssh root@152.42.213.98
cd /root/arbitrage-trading
git revert HEAD   # or git reset --hard <last good commit hash>
sudo systemctl restart layer2
```
The last known-good commit before refactor is: **9b0c627**
