"""
Layer 2 — Logic Core

Receives clean signals from Layer 1, calculates position sizes, and
dispatches execution tickets to Layer 3 workers via ZeroMQ PUSH.

Telegram bot responsibilities:
  - /changepropfirm : 8-step wizard — collects prop firm config and auto-applies buffers
  - /phase1 /phase2  : phase multiplier control
  - /stop /resume    : signal processing gate
  - /status          : live system status
  - /propfirm        : show current prop firm config

Equity monitor (background thread, 30 s interval):
  - Kill 1 (all phases) : daily loss ≥ max_drawdown_daily_pct     → FORCE_CLOSE + halt
  - Kill 2 (all phases) : overall loss ≥ max_drawdown_overall_pct → FORCE_CLOSE + halt  [static vs baseline]
  - Kill 3 (Phase 2)    : daily profit ≥ daily_profit_cap_pct     → FORCE_CLOSE + halt
  - Kill 4 (Phase 1)    : overall profit ≥ profit_target_pct      → FORCE_CLOSE + permanent halt

SGT kill switch (enforced inline in /signal endpoint):
  - Rejects signals 00:00–08:59 SGT and on weekends
  - Dispatches FORCE_CLOSE once per day at the curfew transition (positions only, no halt)

Environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — required
"""

import asyncio
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import zmq
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters as tg_filters,
)

# ── Logging ───────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_log_file = LOG_DIR / f"layer2_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger("layer2")

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT                 = Path(__file__).parent.parent
PHASE_CONFIG_PATH    = ROOT / "config" / "phase_config.json"
RISK_PARAMS_PATH     = ROOT / "config" / "risk_params.json"
PROPFIRM_CONFIG_PATH = ROOT / "config" / "propfirm_config.json"

# ── Timezone ──────────────────────────────────────────────────────────────
SGT = ZoneInfo("Asia/Singapore")

# ── Env vars ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = int(os.environ["TELEGRAM_CHAT_ID"])

# ── Risk params ───────────────────────────────────────────────────────────
with RISK_PARAMS_PATH.open() as _f:
    _risk = json.load(_f)

PROP_RISK_PCT  = float(_risk["prop_risk_pct"])
PHASE_MULT     = {int(k): float(v) for k, v in _risk["phase_multipliers"].items()}
ZMQ_PUSH_PROP  = _risk["layer3_zmq"]["prop"]["push"]
ZMQ_PUSH_PERS  = _risk["layer3_zmq"]["personal"]["push"]
ZMQ_REQ_PROP   = _risk["layer3_zmq"]["prop"]["rep"]
ZMQ_REQ_PERS   = _risk["layer3_zmq"]["personal"]["rep"]
EQUITY_TIMEOUT = 3_000  # ms

ALLOWED_PAIRS: frozenset[str] = frozenset({
    "XAUUSD", "USDJPY", "BTCUSD", "ETHUSD", "FTSE100",
})

# RR constants — immutable across all phases
_RR_PERSONAL = 0.27
_RR_PROP     = 1.0 / _RR_PERSONAL   # ≈ 3.7037

# ── Shared state ──────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_pf_lock    = threading.Lock()


def _load_phase() -> dict:
    with PHASE_CONFIG_PATH.open() as f:
        return json.load(f)


def _save_phase(data: dict) -> None:
    with PHASE_CONFIG_PATH.open("w") as f:
        json.dump(data, f, indent=2)


def _load_propfirm() -> dict:
    with PROPFIRM_CONFIG_PATH.open() as f:
        return json.load(f)


def _save_propfirm(data: dict) -> None:
    with PROPFIRM_CONFIG_PATH.open("w") as f:
        json.dump(data, f, indent=2)


_phase_state: dict = _load_phase()
_propfirm:    dict = _load_propfirm()

# ── ZeroMQ ────────────────────────────────────────────────────────────────
_zmq_ctx = zmq.Context.instance()


def _query_equity(zmq_url: str, ticker: str) -> dict:
    """Query Layer 3 worker. Returns dict with keys:
      balance, equity, point, contract_size, trade_tick_value, digits

    Pass ticker="" for balance/equity-only queries (monitor, baseline lock).
    Pass the canonical ticker (e.g. "BTCUSD") for signal handler contract queries.
    """
    sock = _zmq_ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(zmq_url)
    try:
        sock.send_json({"query": "equity", "ticker": ticker})
        if not sock.poll(EQUITY_TIMEOUT):
            raise RuntimeError(f"equity query timed out ({zmq_url})")
        reply = sock.recv_json()
        if "balance" not in reply:
            raise RuntimeError(f"bad reply from {zmq_url}: {reply}")
        return {
            "balance":          float(reply.get("balance",          0.0)),
            "equity":           float(reply.get("equity",           0.0)),
            "point":            float(reply.get("point",            0.0)),
            "contract_size":    float(reply.get("contract_size",    0.0)),
            "trade_tick_value": float(reply.get("trade_tick_value", 0.0)),
            "digits":           int(reply.get("digits",             5)),
        }
    finally:
        sock.close()


def _push_ticket(zmq_url: str, ticket: dict) -> None:
    sock = _zmq_ctx.socket(zmq.PUSH)
    sock.setsockopt(zmq.LINGER, 1_000)
    sock.connect(zmq_url)
    try:
        sock.send_json(ticket)
    finally:
        sock.close()


def _invert(signal: str) -> str:
    return "SHORT" if signal == "LONG" else "LONG"


# ── Buffer logic ──────────────────────────────────────────────────────────

def _apply_buffers(raw: dict) -> dict:
    """Apply safety buffers to raw prop firm limits.

    - Loss limits: subtract 1 percentage point each.
    - Daily profit cap: enforce at 25% of target (vs the 30% consistency rule).
    """
    effective = raw.copy()
    effective["max_drawdown_daily_pct"]   = round(raw["max_drawdown_daily_pct"]   - 1.0, 2)
    effective["max_drawdown_overall_pct"] = round(raw["max_drawdown_overall_pct"] - 1.0, 2)
    effective["daily_profit_cap_pct"]     = round(raw["profit_target_pct"] * 0.25, 2)
    return effective


# ── SGT helpers ───────────────────────────────────────────────────────────

def _sgt_now() -> datetime:
    return datetime.now(SGT)


def _propfirm_day(now_sgt: datetime) -> str:
    """Return the prop firm trading-day label for a given SGT moment.

    The prop firm resets at 11:00 SGT daily. Any time before 11:00 SGT belongs
    to the trading day that opened the previous calendar day at 11:00 SGT.
    """
    if now_sgt.hour < 11:
        return (now_sgt.date() - timedelta(days=1)).isoformat()
    return now_sgt.date().isoformat()


def _is_sgt_curfew() -> bool:
    now = _sgt_now()
    return now.hour < 9 or now.weekday() >= 5


# ── Telegram alert (sync — safe to call from any thread) ──────────────────

def _alert_sync(message: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        with httpx.Client(timeout=5.0) as client:
            client.post(url, json={"chat_id": CHAT_ID, "text": f"TEE Alert\n{message}"})
    except Exception as exc:
        logger.error("Telegram sync alert failed: %s", exc)


async def _telegram_alert(message: str) -> None:
    try:
        bot = Bot(token=BOT_TOKEN)
        async with bot:
            await bot.send_message(chat_id=CHAT_ID, text=f"TEE Alert\n{message}")
    except Exception as exc:
        logger.error("Telegram async alert failed: %s", exc)


# ── Force-close dispatch ──────────────────────────────────────────────────

def _dispatch_force_close(reason: str, *, halt: bool = True, permanent: bool = False) -> None:
    """Push FORCE_CLOSE to both Layer 3 workers.

    halt=True     — also sets active=False in phase config (for kill conditions).
    halt=False    — positions closed only; active flag untouched (for SGT curfew).
    permanent=True — sets phase1_permanently_halted (Phase 1 target reached).
    """
    ticket = {"action": "FORCE_CLOSE", "reason": reason}
    for url in (ZMQ_PUSH_PROP, ZMQ_PUSH_PERS):
        try:
            _push_ticket(url, ticket)
        except Exception as exc:
            logger.error("FORCE_CLOSE dispatch failed → %s: %s", url, exc)

    if halt:
        with _state_lock:
            _phase_state["active"] = False
            if permanent:
                _phase_state["phase1_permanently_halted"] = True
            _save_phase(_phase_state)

    logger.warning("FORCE_CLOSE dispatched — reason=%s  halt=%s  permanent=%s",
                   reason, halt, permanent)


# ── Equity monitoring ─────────────────────────────────────────────────────

_last_curfew_close_date: date | None = None


def _update_day_start(equity: float) -> None:
    with _pf_lock:
        _propfirm["day_start_equity"]   = equity
        _propfirm["day_start_date_utc"] = _propfirm_day(_sgt_now())
        _save_propfirm(_propfirm)
    logger.info("Day-start equity set to %.2f", equity)


def _lock_baseline_from_live() -> tuple[float, str]:
    """Query live MT5 balance and lock it as baseline_equity + day_start_equity.

    Called synchronously — intended for asyncio.to_thread() from Telegram handlers.
    Returns (balance, error_message). error_message is empty on success.
    """
    try:
        result  = _query_equity(ZMQ_REQ_PROP, "")   # balance-only; no contract info needed
        balance = result["balance"]
    except Exception as exc:
        return 0.0, str(exc)

    today = _propfirm_day(_sgt_now())
    with _pf_lock:
        _propfirm["baseline_equity"]    = balance
        _propfirm["day_start_equity"]   = balance
        _propfirm["day_start_date_utc"] = today
        _save_propfirm(_propfirm)

    logger.info("Baseline locked from live MT5 balance: %.2f", balance)
    return balance, ""


def _dispatch_parameters() -> None:
    """Push static DD floor to the prop worker so its independent guard stays in sync.

    Called after any event that changes baseline_equity (phase commands, wizard confirm).
    """
    with _pf_lock:
        pf = dict(_propfirm)

    baseline = pf.get("baseline_equity", 0.0)
    dd_pct   = pf.get("max_drawdown_overall_pct", 0.0)

    if baseline <= 0 or dd_pct <= 0:
        logger.info("SET_PARAMETERS skipped — baseline or overall DD limit not configured")
        return

    floor = round(baseline * (1.0 - dd_pct / 100.0), 2)
    msg   = {"action": "SET_PARAMETERS", "dd_floor": floor}
    try:
        _push_ticket(ZMQ_PUSH_PROP, msg)
        logger.info("SET_PARAMETERS → prop: baseline=%.2f  dd_pct=%.2f%%  floor=%.2f",
                    baseline, dd_pct, floor)
    except Exception as exc:
        logger.error("SET_PARAMETERS dispatch failed: %s", exc)


def _equity_monitor_loop() -> None:
    while True:
        time.sleep(30)
        try:
            _run_equity_check()
        except Exception as exc:
            logger.error("Equity monitor error: %s", exc)


def _run_equity_check() -> None:
    global _last_curfew_close_date

    with _state_lock:
        p1_halt = _phase_state.get("phase1_permanently_halted", False)
    if p1_halt:
        return

    now_sgt  = _sgt_now()
    curfew   = now_sgt.hour < 9 or now_sgt.weekday() >= 5
    today    = now_sgt.date()

    if curfew:
        if _last_curfew_close_date != today:
            logger.info("Monitor: SGT curfew transition — dispatching force-close (positions only)")
            _dispatch_force_close("sgt_curfew", halt=False)
            _alert_sync("SGT curfew — all positions closed. Resumes 09:00 SGT on next weekday.")
            _last_curfew_close_date = today
        return

    with _state_lock:
        active = _phase_state.get("active", False)
        phase  = int(_phase_state.get("phase", 1))

    if not active:
        return

    try:
        _eq_result  = _query_equity(ZMQ_REQ_PROP, "")   # balance + equity only
        prop_equity = _eq_result["equity"]
    except Exception as exc:
        logger.warning("Monitor: prop equity query failed: %s", exc)
        return

    with _pf_lock:
        pf = dict(_propfirm)

    # Reset day-start equity when the prop firm's 11:00 SGT window rolls over
    stored_date = pf.get("day_start_date_utc", "")
    if stored_date != _propfirm_day(now_sgt):
        _update_day_start(prop_equity)
        return

    day_start = pf.get("day_start_equity", 0.0)
    baseline  = pf.get("baseline_equity",  0.0)

    if day_start == 0.0:
        _update_day_start(prop_equity)
        return

    # Kill 1 — daily loss (all phases) — measured from day_start_equity
    daily_loss_pct = (day_start - prop_equity) / day_start * 100
    if daily_loss_pct >= pf["max_drawdown_daily_pct"] > 0:
        msg = (f"KILL 1 — Daily loss limit hit\n"
               f"Daily loss: {daily_loss_pct:.2f}% ≥ {pf['max_drawdown_daily_pct']}%\n"
               f"Equity: {prop_equity:.2f}  |  Use /resume tomorrow.")
        logger.warning(msg)
        _dispatch_force_close("daily_loss_limit", halt=True)
        _alert_sync(msg)
        return

    # Kill 2 — overall static drawdown (all phases) — measured from baseline_equity
    # The floor is a hard absolute value; it never trails profits.
    if baseline > 0:
        overall_dd_limit = pf.get("max_drawdown_overall_pct", 0.0)
        if overall_dd_limit > 0:
            overall_loss_pct = (baseline - prop_equity) / baseline * 100
            if overall_loss_pct >= overall_dd_limit:
                floor = round(baseline * (1.0 - overall_dd_limit / 100.0), 2)
                msg = (f"KILL 2 — Overall static drawdown limit hit\n"
                       f"Overall loss: {overall_loss_pct:.2f}% ≥ {overall_dd_limit}%\n"
                       f"Baseline: {baseline:.2f}  |  Floor: {floor:.2f}  |  "
                       f"Equity: {prop_equity:.2f}\n"
                       f"Use /resume after review.")
                logger.warning(msg)
                _dispatch_force_close("overall_drawdown_limit", halt=True)
                _alert_sync(msg)
                return

    # Kill 3 — daily profit cap (Phase 2 only) — measured from day_start_equity
    if phase == 2:
        daily_profit_pct = (prop_equity - day_start) / day_start * 100
        cap = pf.get("daily_profit_cap_pct", 0.0)
        if cap > 0 and daily_profit_pct >= cap:
            msg = (f"KILL 3 — Daily profit cap hit (Phase 2)\n"
                   f"Daily profit: {daily_profit_pct:.2f}% ≥ {cap}%\n"
                   f"Equity: {prop_equity:.2f}  |  Use /resume tomorrow.")
            logger.warning(msg)
            _dispatch_force_close("daily_profit_cap", halt=True)
            _alert_sync(msg)
            return

    # Kill 4 — Phase 1 profit target — cumulative from baseline
    if phase == 1 and baseline > 0:
        overall_pct = (prop_equity - baseline) / baseline * 100
        target      = pf.get("profit_target_pct", 0.0)
        if target > 0 and overall_pct >= target:
            msg = (f"KILL 4 — Phase 1 profit target reached!\n"
                   f"Overall profit: {overall_pct:.2f}% ≥ {target}%\n"
                   f"Equity: {prop_equity:.2f}\n"
                   f"System PERMANENTLY HALTED.\n"
                   f"Use /phase2 then /resume to continue.")
            logger.warning(msg)
            _dispatch_force_close("phase1_target", halt=True, permanent=True)
            _alert_sync(msg)


# ── Telegram wizard — /changepropfirm ────────────────────────────────────

(PF_NAME, PF_PROFIT_TARGET, PF_MAX_DD_OVERALL, PF_MAX_DD_DAILY,
 PF_DD_TYPE, PF_RAW_SPREAD, PF_PROFIT_SHARE, PF_MIN_DAYS, PF_CONFIRM) = range(9)

_wizard_data: dict = {}


def _auth(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == CHAT_ID


async def _cmd_changepropfirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text(
        "Change Prop Firm Config\n\n"
        "Step 1/8 — Enter Prop Firm Name:"
    )
    return PF_NAME


async def _wiz_name(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _wizard_data["propfirm_name"] = update.message.text.strip()
    await update.message.reply_text("Step 2/8 — Profit Target % (firm's number, e.g. 10):")
    return PF_PROFIT_TARGET


async def _wiz_profit_target(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("Invalid — enter a positive number (e.g. 10):")
        return PF_PROFIT_TARGET
    _wizard_data["profit_target_pct"] = v
    await update.message.reply_text("Step 3/8 — Max Drawdown Overall % (firm's raw limit, e.g. 10):")
    return PF_MAX_DD_OVERALL


async def _wiz_max_dd_overall(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("Invalid — enter a positive number (e.g. 10):")
        return PF_MAX_DD_OVERALL
    _wizard_data["max_drawdown_overall_pct"] = v
    await update.message.reply_text("Step 4/8 — Max Drawdown Daily % (firm's raw limit, e.g. 3):")
    return PF_MAX_DD_DAILY


async def _wiz_max_dd_daily(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("Invalid — enter a positive number (e.g. 3):")
        return PF_MAX_DD_DAILY
    _wizard_data["max_drawdown_daily_pct"] = v
    await update.message.reply_text(
        "Step 5/8 — Drawdown type?\nType: static  or  dynamic"
    )
    return PF_DD_TYPE


async def _wiz_dd_type(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()

    # Confirmation step — user previously entered "dynamic" and was warned
    if _wizard_data.get("_dd_type_confirming"):
        if v.upper() == "CONFIRM":
            _wizard_data["drawdown_is_static"] = False
            _wizard_data.pop("_dd_type_confirming")
            await update.message.reply_text(
                "Dynamic drawdown accepted (flagged).\n"
                "Step 6/8 — Raw spread account?\nType: yes  or  no"
            )
            return PF_RAW_SPREAD
        else:
            _wizard_data.pop("_dd_type_confirming")
            await update.message.reply_text(
                "Confirmation not received — please re-enter drawdown type.\n"
                "Type: static  or  dynamic"
            )
            return PF_DD_TYPE

    v_lower = v.lower()
    if v_lower == "static":
        _wizard_data["drawdown_is_static"] = True
        await update.message.reply_text(
            "Step 6/8 — Raw spread account?\nType: yes  or  no"
        )
        return PF_RAW_SPREAD
    elif v_lower == "dynamic":
        _wizard_data["_dd_type_confirming"] = True
        await update.message.reply_text(
            "WARNING — Dynamic drawdown flagged.\n"
            "This system is built for static drawdown accounts.\n"
            "Reply CONFIRM to accept, or type 'static' to correct."
        )
        return PF_DD_TYPE
    else:
        await update.message.reply_text("Type exactly:  static  or  dynamic")
        return PF_DD_TYPE


async def _wiz_raw_spread(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()

    # Confirmation step — user previously entered "no" and was warned
    if _wizard_data.get("_raw_spread_confirming"):
        if v.upper() == "CONFIRM":
            _wizard_data["raw_spread_account"] = False
            _wizard_data.pop("_raw_spread_confirming")
            await update.message.reply_text(
                "Non-raw spread accepted (flagged).\n"
                "Step 7/8 — Profit Sharing % (e.g. 80):"
            )
            return PF_PROFIT_SHARE
        else:
            _wizard_data.pop("_raw_spread_confirming")
            await update.message.reply_text(
                "Confirmation not received — please re-enter.\n"
                "Type: yes  or  no"
            )
            return PF_RAW_SPREAD

    v_lower = v.lower()
    if v_lower == "yes":
        _wizard_data["raw_spread_account"] = True
        await update.message.reply_text("Step 7/8 — Profit Sharing % (e.g. 80):")
        return PF_PROFIT_SHARE
    elif v_lower == "no":
        _wizard_data["_raw_spread_confirming"] = True
        await update.message.reply_text(
            "WARNING — Non-raw spread account flagged.\n"
            "This system is built for raw spread accounts.\n"
            "Reply CONFIRM to accept, or type 'yes' to correct."
        )
        return PF_RAW_SPREAD
    else:
        await update.message.reply_text("Type exactly:  yes  or  no")
        return PF_RAW_SPREAD


async def _wiz_profit_share(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert 0 < v <= 100
    except Exception:
        await update.message.reply_text("Invalid — enter a number between 1 and 100:")
        return PF_PROFIT_SHARE
    _wizard_data["profit_sharing_pct"] = v
    await update.message.reply_text("Step 8/8 — Minimum Profit Days (e.g. 5):")
    return PF_MIN_DAYS


async def _wiz_min_days(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = int(update.message.text.strip())
        assert v >= 0
    except Exception:
        await update.message.reply_text("Invalid — enter a whole number (e.g. 5):")
        return PF_MIN_DAYS
    _wizard_data["min_profit_days"] = v

    eff = _apply_buffers(_wizard_data)
    dd_flag  = "  *** FLAGGED — non-standard" if not _wizard_data["drawdown_is_static"] else ""
    rs_flag  = "  *** FLAGGED — non-standard" if not _wizard_data["raw_spread_account"] else ""
    summary = (
        f"Review Before Saving\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Firm              : {_wizard_data['propfirm_name']}\n"
        f"Profit Target     : {_wizard_data['profit_target_pct']}%\n"
        f"Max DD Overall    : {_wizard_data['max_drawdown_overall_pct']}%  "
        f"→ enforced at {eff['max_drawdown_overall_pct']}%  (−1pp buffer)\n"
        f"Max DD Daily      : {_wizard_data['max_drawdown_daily_pct']}%  "
        f"→ enforced at {eff['max_drawdown_daily_pct']}%  (−1pp buffer)\n"
        f"Drawdown Type     : {'Static' if _wizard_data['drawdown_is_static'] else 'Dynamic'}{dd_flag}\n"
        f"Raw Spread Acct   : {'Yes' if _wizard_data['raw_spread_account'] else 'No'}{rs_flag}\n"
        f"Profit Sharing    : {_wizard_data['profit_sharing_pct']}%\n"
        f"Min Profit Days   : {_wizard_data['min_profit_days']}\n"
        f"\n"
        f"Kill conditions:\n"
        f"  Kill 1 — daily loss ≥ {eff['max_drawdown_daily_pct']}% → close all + halt\n"
        f"  Kill 2 — overall loss ≥ {eff['max_drawdown_overall_pct']}% from baseline (static) → close all + halt\n"
        f"  Kill 3 — daily profit ≥ {eff['daily_profit_cap_pct']}% (Phase 2) → close all + halt\n"
        f"  Kill 4 — overall profit ≥ {_wizard_data['profit_target_pct']}% (Phase 1) → close all + permanent halt\n"
        f"\n"
        f"Baseline equity fetched live from MT5 on confirm.\n"
        f"\nReply YES to save  |  NO to cancel."
    )
    await update.message.reply_text(summary)
    return PF_CONFIRM


async def _wiz_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip().upper()
    if v == "NO":
        _wizard_data.clear()
        await update.message.reply_text("Cancelled — no changes saved.")
        return ConversationHandler.END
    if v != "YES":
        await update.message.reply_text("Reply YES to save or NO to cancel.")
        return PF_CONFIRM

    eff = _apply_buffers(_wizard_data)

    baseline = 0.0
    try:
        baseline = _query_equity(ZMQ_REQ_PROP, "")["balance"]
    except Exception as exc:
        await update.message.reply_text(
            f"Warning: could not fetch live balance ({exc}).\n"
            f"Baseline set to 0.0 — run /changepropfirm again once MT5 is connected."
        )

    with _pf_lock:
        _propfirm.update({
            "propfirm_name":            _wizard_data["propfirm_name"],
            "profit_target_pct":        _wizard_data["profit_target_pct"],
            "max_drawdown_overall_pct": eff["max_drawdown_overall_pct"],
            "max_drawdown_daily_pct":   eff["max_drawdown_daily_pct"],
            "drawdown_is_static":       _wizard_data["drawdown_is_static"],
            "raw_spread_account":       _wizard_data["raw_spread_account"],
            "profit_sharing_pct":       _wizard_data["profit_sharing_pct"],
            "min_profit_days":          _wizard_data["min_profit_days"],
            "daily_profit_cap_pct":     eff["daily_profit_cap_pct"],
            "baseline_equity":          baseline,
            "day_start_equity":         baseline,
            "day_start_date_utc":       _propfirm_day(_sgt_now()),
        })
        _save_propfirm(_propfirm)

    if baseline > 0:
        _dispatch_parameters()

    _wizard_data.clear()
    await update.message.reply_text(
        f"Saved.\n"
        f"Firm         : {_propfirm['propfirm_name']}\n"
        f"Baseline eq  : {baseline:.2f}\n"
        f"All kill-switch thresholds are now active."
    )
    logger.info("Prop firm config updated — firm=%s  baseline=%.2f",
                _propfirm["propfirm_name"], baseline)
    return ConversationHandler.END


async def _wiz_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text("Wizard cancelled.")
    return ConversationHandler.END


# ── Telegram commands ─────────────────────────────────────────────────────

async def _cmd_phase1(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        _phase_state["phase"] = 1
        _phase_state.pop("phase1_permanently_halted", None)
        _save_phase(_phase_state)

    balance, err = await asyncio.to_thread(_lock_baseline_from_live)
    if err:
        await update.message.reply_text(
            f"Phase 1 set — personal lots ×{PHASE_MULT[1]:.2f}\n"
            f"Warning: could not fetch live balance ({err}).\n"
            f"Baseline NOT updated — run /phase1 again once MT5 is connected."
        )
        logger.warning("Telegram /phase1: baseline lock failed: %s", err)
        return

    await asyncio.to_thread(_dispatch_parameters)
    await update.message.reply_text(
        f"Phase 1 set — personal lots ×{PHASE_MULT[1]:.2f}\n"
        f"Baseline locked: {balance:.2f}"
    )
    logger.info("Telegram: phase set to 1  baseline=%.2f", balance)


async def _cmd_phase2(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        _phase_state["phase"] = 2
        _phase_state.pop("phase1_permanently_halted", None)
        _save_phase(_phase_state)

    balance, err = await asyncio.to_thread(_lock_baseline_from_live)
    if err:
        await update.message.reply_text(
            f"Phase 2 set — personal lots ×{PHASE_MULT[2]:.2f}\n"
            f"Phase 1 permanent halt cleared.\n"
            f"Warning: could not fetch live balance ({err}).\n"
            f"Baseline NOT updated — run /phase2 again once MT5 is connected."
        )
        logger.warning("Telegram /phase2: baseline lock failed: %s", err)
        return

    await asyncio.to_thread(_dispatch_parameters)
    await update.message.reply_text(
        f"Phase 2 set — personal lots ×{PHASE_MULT[2]:.2f}\n"
        f"Phase 1 permanent halt cleared.\n"
        f"Baseline locked: {balance:.2f}"
    )
    logger.info("Telegram: phase set to 2  baseline=%.2f", balance)


async def _cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        _phase_state["active"] = False
        _save_phase(_phase_state)
    await update.message.reply_text("Signal processing HALTED. Send /resume to re-enable.")
    logger.warning("Telegram: halted by user")


async def _cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        p1_halt = _phase_state.get("phase1_permanently_halted", False)
    if p1_halt:
        await update.message.reply_text(
            "Phase 1 profit target was reached.\nYou must /phase2 before resuming."
        )
        return
    with _state_lock:
        _phase_state["active"] = True
        _save_phase(_phase_state)
    curfew_note = "  (Note: SGT curfew active — signals will queue until 09:00 SGT)" if _is_sgt_curfew() else ""
    await update.message.reply_text(f"Signal processing RESUMED.{curfew_note}")
    logger.info("Telegram: resumed by user")


async def _cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        phase   = _phase_state.get("phase", "?")
        active  = _phase_state.get("active", False)
        last_ts = _phase_state.get("last_signal_ts", "never")
        p1_halt = _phase_state.get("phase1_permanently_halted", False)
    with _pf_lock:
        pf_name    = _propfirm.get("propfirm_name", "not configured")
        day_start  = _propfirm.get("day_start_equity",        0.0)
        baseline   = _propfirm.get("baseline_equity",         0.0)
        dd_daily   = _propfirm.get("max_drawdown_daily_pct",  0.0)
        dd_overall = _propfirm.get("max_drawdown_overall_pct", 0.0)
        cap        = _propfirm.get("daily_profit_cap_pct",    0.0)
    floor  = round(baseline * (1.0 - dd_overall / 100.0), 2) if baseline > 0 and dd_overall > 0 else 0.0
    mult   = PHASE_MULT.get(phase, "?")
    curfew = _is_sgt_curfew()
    await update.message.reply_text(
        f"Phase          : {phase}  (×{mult})\n"
        f"Active         : {'YES' if active else 'NO — halted'}\n"
        f"P1 Perm Halt   : {'YES — /phase2 required' if p1_halt else 'No'}\n"
        f"SGT Curfew     : {'YES (dormant)' if curfew else 'No'}\n"
        f"Firm           : {pf_name}\n"
        f"Baseline eq    : {baseline:.2f}\n"
        f"Static DD floor: {floor:.2f}  (−{dd_overall}% from baseline)\n"
        f"Day-start eq   : {day_start:.2f}\n"
        f"Daily DD limit : {dd_daily}%\n"
        f"Daily profit ↑ : {cap}%\n"
        f"Last signal    : {last_ts}"
    )


async def _cmd_propfirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _pf_lock:
        pf = dict(_propfirm)
    await update.message.reply_text(
        f"Prop Firm Config\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Firm              : {pf.get('propfirm_name', '—')}\n"
        f"Profit Target     : {pf.get('profit_target_pct', 0)}%\n"
        f"Max DD Overall    : {pf.get('max_drawdown_overall_pct', 0)}%  (buffered)\n"
        f"Max DD Daily      : {pf.get('max_drawdown_daily_pct', 0)}%   (buffered)\n"
        f"Drawdown Type     : {'Static' if pf.get('drawdown_is_static') else 'Dynamic'}\n"
        f"Raw Spread Acct   : {'Yes' if pf.get('raw_spread_account') else 'No'}\n"
        f"Profit Sharing    : {pf.get('profit_sharing_pct', 0)}%\n"
        f"Min Profit Days   : {pf.get('min_profit_days', 0)}\n"
        f"Daily Profit Cap  : {pf.get('daily_profit_cap_pct', 0)}%\n"
        f"Baseline Equity   : {pf.get('baseline_equity', 0):.2f}\n"
        f"Day-Start Equity  : {pf.get('day_start_equity', 0):.2f}"
    )


# ── Bot startup ───────────────────────────────────────────────────────────

def _run_bot() -> None:
    wizard = ConversationHandler(
        entry_points=[CommandHandler("changepropfirm", _cmd_changepropfirm)],
        states={
            PF_NAME:           [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_name)],
            PF_PROFIT_TARGET:  [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_profit_target)],
            PF_MAX_DD_OVERALL: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_max_dd_overall)],
            PF_MAX_DD_DAILY:   [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_max_dd_daily)],
            PF_DD_TYPE:        [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_dd_type)],
            PF_RAW_SPREAD:     [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_raw_spread)],
            PF_PROFIT_SHARE:   [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_profit_share)],
            PF_MIN_DAYS:       [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_min_days)],
            PF_CONFIRM:        [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _wiz_cancel)],
        per_chat=True,
    )

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(wizard)
    tg_app.add_handler(CommandHandler("phase1",        _cmd_phase1))
    tg_app.add_handler(CommandHandler("phase2",        _cmd_phase2))
    tg_app.add_handler(CommandHandler("stop",          _cmd_stop))
    tg_app.add_handler(CommandHandler("resume",        _cmd_resume))
    tg_app.add_handler(CommandHandler("status",        _cmd_status))
    tg_app.add_handler(CommandHandler("propfirm",      _cmd_propfirm))
    tg_app.add_handler(CommandHandler("changepropfirm", _cmd_changepropfirm))

    async def _poll():
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=["message"])
        logger.info("Telegram bot polling (chat_id=%d)", CHAT_ID)
        await asyncio.Event().wait()  # block forever; thread is daemon so exits with process

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_poll())


threading.Thread(target=_run_bot,              daemon=True, name="tg-bot").start()
threading.Thread(target=_equity_monitor_loop,  daemon=True, name="equity-monitor").start()

# ── FastAPI ───────────────────────────────────────────────────────────────

app = FastAPI(title="TEE Layer 2 — Logic Core", version="2.0.0")


class SignalPayload(BaseModel):
    signal:         str
    ticker:         str
    timestamp_ms:   int
    timeframe:      str
    entry:          float
    sl:             float
    tp:             float
    sl_pips:        float
    rr_ratio:       float
    order_type:     str
    daily_trend:    str
    m15_swing_high: float
    m15_swing_low:  float
    pip_type:       str

    @field_validator("signal")
    @classmethod
    def _val_signal(cls, v: str) -> str:
        v = v.upper()
        if v not in ("LONG", "SHORT"):
            raise ValueError(f"unexpected signal '{v}'")
        return v

    @field_validator("ticker")
    @classmethod
    def _val_ticker(cls, v: str) -> str:
        v = v.upper()
        if v not in ALLOWED_PAIRS:
            raise ValueError(f"ticker '{v}' not in allowed pairs")
        return v


@app.post("/signal")
async def receive_signal(request: Request):
    raw = await request.body()

    try:
        payload = SignalPayload.model_validate_json(raw)
    except Exception as exc:
        logger.warning("Malformed payload: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))

    # SGT curfew / weekend gate — no state change, just reject inline
    if _is_sgt_curfew():
        now_sgt = _sgt_now()
        reason  = "weekend" if now_sgt.weekday() >= 5 else "SGT curfew 00:00–09:00"
        logger.info("GATE %s %s — %s", payload.signal, payload.ticker, reason)
        return JSONResponse({"status": "rejected", "reason": reason})

    with _state_lock:
        active  = _phase_state.get("active", False)
        phase   = int(_phase_state.get("phase", 1))
        p1_halt = _phase_state.get("phase1_permanently_halted", False)

    if p1_halt:
        return JSONResponse({
            "status": "halted",
            "reason": "phase1 target reached — /phase2 then /resume to continue",
        })

    if not active:
        logger.info("HALTED — dropped %s %s", payload.signal, payload.ticker)
        return JSONResponse({"status": "halted", "reason": "signal processing stopped"})

    logger.info("SIGNAL  %s %s | entry=%.5f  sl_pips=%.1f  phase=%d",
                payload.signal, payload.ticker, payload.entry, payload.sl_pips, phase)

    try:
        prop_info = await asyncio.to_thread(
            _query_equity, ZMQ_REQ_PROP, payload.ticker
        )
    except Exception as exc:
        msg = f"Prop contract query failed: {exc}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    try:
        pers_info = await asyncio.to_thread(
            _query_equity, ZMQ_REQ_PERS, payload.ticker
        )
    except Exception as exc:
        msg = f"Personal contract query failed: {exc}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    # Step A — prop dollar risk: strictly 0.67% of static baseline (never live equity)
    with _pf_lock:
        baseline_equity = _propfirm.get("baseline_equity", 0.0)
    if baseline_equity <= 0:
        msg = "baseline_equity not set — send /phase1 or /phase2 via Telegram first"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    prop_dollar_risk = baseline_equity * PROP_RISK_PCT
    # Step B+C — personal dollar risk scales by phase ratio
    phase_ratio      = PHASE_MULT.get(phase, PHASE_MULT[1])
    pers_dollar_risk = prop_dollar_risk * phase_ratio

    # Step D — universal contract math: lots = dollar_risk / (sl_distance/point × tick_value)
    sl_distance = abs(payload.entry - payload.sl)

    prop_point    = prop_info["point"]
    prop_tick_val = prop_info["trade_tick_value"]
    pers_point    = pers_info["point"]
    pers_tick_val = pers_info["trade_tick_value"]

    if prop_point <= 0 or prop_tick_val <= 0:
        msg = f"Invalid contract data from prop worker for {payload.ticker} — point={prop_point} tick_value={prop_tick_val}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    prop_dollar_per_lot = (sl_distance / prop_point) * prop_tick_val
    pers_dollar_per_lot = (sl_distance / pers_point) * pers_tick_val

    prop_lots = round(prop_dollar_risk / prop_dollar_per_lot, 2)
    pers_lots = round(pers_dollar_risk / pers_dollar_per_lot, 2)

    # Price rounding: use MT5 digits from prop broker
    price_digits = prop_info["digits"]

    if payload.signal == "LONG":
        prop_tp = round(payload.entry + sl_distance * _RR_PROP,     price_digits)
        pers_sl = payload.m15_swing_high   # swing high above entry = SHORT stop
        pers_tp = round(payload.entry - sl_distance * _RR_PERSONAL, price_digits)
    else:  # SHORT
        prop_tp = round(payload.entry - sl_distance * _RR_PROP,     price_digits)
        pers_sl = payload.m15_swing_low    # swing low below entry = LONG stop
        pers_tp = round(payload.entry + sl_distance * _RR_PERSONAL, price_digits)

    logger.info(
        "LOTS  prop=%.2f lots ($%.2f risk)  personal=%.2f lots ($%.2f risk)  "
        "phase=%d ×%.2f  baseline=%.2f  sl_dist=%.5f  "
        "prop point=%.5f tick=%.4f  pers point=%.5f tick=%.4f",
        prop_lots, prop_dollar_risk, pers_lots, pers_dollar_risk,
        phase, phase_ratio, baseline_equity, sl_distance,
        prop_point, prop_tick_val, pers_point, pers_tick_val,
    )

    prop_ticket = {
        "ticker":       payload.ticker,
        "timestamp_ms": payload.timestamp_ms,
        "entry":        payload.entry,
        "sl":           payload.sl,
        "tp":           prop_tp,
        "sl_pips":      payload.sl_pips,
        "signal":       payload.signal,
        "lots":         prop_lots,
    }
    pers_ticket = {
        "ticker":       payload.ticker,
        "timestamp_ms": payload.timestamp_ms,
        "entry":        payload.entry,
        "sl":           pers_sl,
        "tp":           pers_tp,
        "sl_pips":      payload.sl_pips,
        "signal":       _invert(payload.signal),
        "lots":         pers_lots,
    }

    try:
        await asyncio.to_thread(_push_ticket, ZMQ_PUSH_PROP, prop_ticket)
        logger.info("DISPATCHED  prop     %s  %.2f lots", prop_ticket["signal"], prop_lots)
    except Exception as exc:
        msg = f"Prop dispatch failed: {exc}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    try:
        await asyncio.to_thread(_push_ticket, ZMQ_PUSH_PERS, pers_ticket)
        logger.info("DISPATCHED  personal %s  %.2f lots", pers_ticket["signal"], pers_lots)
    except Exception as exc:
        msg = f"Personal dispatch failed: {exc}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    with _state_lock:
        _phase_state["last_signal_ts"] = datetime.now(timezone.utc).isoformat()
        _save_phase(_phase_state)

    return JSONResponse({
        "status":           "dispatched",
        "ticker":           payload.ticker,
        "baseline_equity":  baseline_equity,
        "prop":             {"signal": prop_ticket["signal"], "lots": prop_lots,
                             "tp": prop_tp, "dollar_risk": prop_dollar_risk},
        "personal":         {"signal": pers_ticket["signal"], "lots": pers_lots,
                             "tp": pers_tp, "dollar_risk": pers_dollar_risk},
        "phase":            phase,
        "phase_ratio":      phase_ratio,
    })


@app.get("/health")
async def health():
    with _state_lock:
        phase  = _phase_state.get("phase")
        active = _phase_state.get("active")
    with _pf_lock:
        pf_name = _propfirm.get("propfirm_name", "")
    return {
        "status":     "ok",
        "layer":      2,
        "phase":      phase,
        "active":     active,
        "propfirm":   pf_name,
        "sgt_curfew": _is_sgt_curfew(),
        "utc_time":   datetime.now(timezone.utc).isoformat(),
    }
