import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from layer1.ff_calendar import fetch_events_sync as _fetch_ff_events
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters as tg_filters,
)

from layer2.state import (
    BOT_TOKEN, CHAT_ID,
    _phase_state, _state_lock,
    _propfirm, _pf_lock,
    _consistency_log, _cons_lock,
    _news_suppressed_pairs, _news_suppressed_lock,
    _manual_suppressed_pairs, _manual_suppress_lock,
    ALLOWED_PAIRS, _TICKER_CURRENCIES, _SYMBOL_MAP,
    _NEWS_TRADING_BAN_WINDOW,
    PHASE_MULT, PROP_RISK_PCT,
    _save_phase, _save_propfirm,
    _reset_consistency_log, _record_day_profit,
    _build_consistency_table,
    _p2_display, _p2_settings_block,
    _P2_FIELD_DEFS, _P2_FIELD_BY_IDX,
    _is_sgt_curfew, _sgt_now, _propfirm_day,
    _apply_buffers, _pnl_bar, _fmt_price, _money,
    _trading_window, _window_lock, _save_trading_window, _window_minutes,
    _phase1_init, _phase1_load,
)
from layer2.zmq_helpers import (
    _query_equity, _query_positions, _snapshot_positions_str,
    _query_checksymbols,
    _dispatch_force_close, _dispatch_close_ticker, _dispatch_news_suppress,
    _dispatch_news_clear, _close_ticker_on_worker,
    _telegram_alert, _alert_sync,
    _lock_baseline_from_live, _dispatch_parameters, _dispatch_fee_anchor_reset,
    ZMQ_REQ_PROP, ZMQ_REQ_PERS, ZMQ_PUSH_PROP, ZMQ_PUSH_PERS,
)
from layer2 import phase1_strategy

logger = logging.getLogger("layer2")

# ── Telegram wizard — /changepropfirm ────────────────────────────────────
# State integer → wizard step mapping:
#   PF_NAME(0)=Step1:ProfitTarget  PF_PROFIT_TARGET(1)=Step2:OverallDD
#   PF_MAX_DD_OVERALL(2)=Step3:DailyDD  PF_MAX_DD_DAILY(3)=Step4:DDType
#   PF_DD_TYPE(4)=Step5:RawSpread  PF_RAW_SPREAD(5)=Step6:ProfitShare
#   PF_PROFIT_SHARE(6)=Step7:MinDays  PF_MIN_DAYS(7)=Step8:Consistency
#   PF_CONSISTENCY(8)=Step9:PropBaseline  PF_INITIAL_BALANCE(9)=Step10:PersBaseline
#   PF_CONFIRM(10)=Review+Save
(PF_NAME, PF_PROFIT_TARGET, PF_MAX_DD_OVERALL, PF_MAX_DD_DAILY,
 PF_DD_TYPE, PF_RAW_SPREAD, PF_PROFIT_SHARE, PF_MIN_DAYS,
 PF_CONSISTENCY, PF_INITIAL_BALANCE, PF_CONFIRM) = range(11)

(P2_SAME_OR_DIFF, P2_WHICH_FIELDS, P2_COLLECTING,
 P2_INITIAL_BALANCE, P2_PERS_BALANCE, P2_CONFIRM) = range(10, 16)

EMERGENCY_CONFIRM    = 16
CLOSEPAIR_CONFIRM    = 17
SETWINDOW_CONFIRM    = 18
UPDATE_LAYER3_CHOOSE = 19
P1_INPUT   = 20
P1_CONFIRM = 21

_wizard_data: dict = {}
_p2_wizard_data: dict = {}
_setwindow_data: dict = {}


def _auth(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == CHAT_ID


def _cmd_header(title: str) -> str:
    """Top + bottom ━ rule bracketing a command-output title.

    Same header format as the alert templates (msg_* functions / _MSG_SEP) so
    on-demand command replies (/positions, /status, …) read identically to the
    bot's pushed alerts. `title` must already include any leading emoji and
    <b></b> tags. Returns the header with a trailing blank line, ready to
    prepend body sections.
    """
    sep = "━" * 12
    return f"{sep}\n{title}\n{sep}\n\n"


def _cmd_pos_block(label: str, positions, err, currency: str = "USD",
                   detail: bool = True, show_pnl: bool = True) -> str:
    """Render one account's open positions as a structured block.

    Shared by /positions, /emergency, /closepair, /stop and /resume so every
    command renders positions the same way.

    detail=True  → per-position aligned rows (Size/Entry/SL/TP[/P&L]).
    detail=False → one compact line per position (symbol · dir · lots[ · P&L]).
    `currency` formats P&L in the account's deposit currency (USD for prop,
    e.g. SGD for personal) so personal-side money is never mislabelled '$'.
    show_pnl=False drops the P&L entirely (used by /stop, /resume).
    """
    if err:
        return f"<b>{label}</b>\nOffline — {err}"
    if not positions:
        return f"<b>{label}</b>\nNo open positions"
    parts = []
    for p in positions:
        arrow = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
        pnl   = _msg_signed_money(p["profit"], currency)
        if detail:
            rows = [
                ("Size",  f"{p['volume']:.2f} lots"),
                ("Entry", _fmt_price(p["symbol"], p["price_open"])),
                ("SL",    _fmt_price(p["symbol"], p["sl"])),
                ("TP",    _fmt_price(p["symbol"], p["tp"])),
            ]
            if show_pnl:
                rows.append(("P&amp;L", pnl))
            parts.append(f"{p['symbol']}  {arrow}\n{_msg_aligned_rows(rows)}")
        else:
            tail = f"  {pnl}" if show_pnl else ""
            parts.append(f"{p['symbol']}  {arrow}  {p['volume']:.2f} lots{tail}")
    body = "\n\n".join(parts) if detail else "\n".join(parts)
    return f"<b>{label}</b>\n{body}"


async def _pers_currency() -> str:
    """Best-effort personal-account deposit currency (SGD on the live Fusion
    account). Falls back to 'USD' if the worker is unreachable. Used to label
    personal-side P&L in command outputs."""
    try:
        pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        return pers.get("account_currency", "USD")
    except Exception:
        return "USD"


async def _cmd_changepropfirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text(
        "🏦 <b>Account Setup</b>\n\n"
        "<b>Step 1/10 — Profit Target</b>\n"
        "Enter the profit target percentage.\n\n"
        "Example: <code>10</code>",
        parse_mode="HTML",
    )
    return PF_NAME


# Step 1: collect profit target → ask Step 2 (Overall DD)
async def _wiz_name(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>10</code>",
            parse_mode="HTML",
        )
        return PF_NAME
    _wizard_data["profit_target_pct"] = v
    await update.message.reply_text(
        "<b>Step 2/10 — Overall Drawdown</b>\n"
        "Enter the overall drawdown limit.\n\n"
        "Example: <code>10</code>",
        parse_mode="HTML",
    )
    return PF_PROFIT_TARGET


# Step 2: collect overall DD → ask Step 3 (Daily DD)
async def _wiz_profit_target(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>10</code>",
            parse_mode="HTML",
        )
        return PF_PROFIT_TARGET
    _wizard_data["max_drawdown_overall_pct"] = v
    await update.message.reply_text(
        "<b>Step 3/10 — Daily Drawdown</b>\n"
        "Enter the daily drawdown limit.\n\n"
        "Example: <code>3</code>\n\n"
        "The bot will apply the configured buffer automatically.",
        parse_mode="HTML",
    )
    return PF_MAX_DD_OVERALL


# Step 3: collect daily DD → ask Step 4 (DD Type)
async def _wiz_max_dd_overall(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>3</code>",
            parse_mode="HTML",
        )
        return PF_MAX_DD_OVERALL
    _wizard_data["max_drawdown_daily_pct"] = v
    await update.message.reply_text(
        "<b>Step 4/10 — Drawdown Type</b>\n"
        "Type: <code>static</code> or <code>dynamic</code>",
        parse_mode="HTML",
    )
    return PF_MAX_DD_DAILY


# Step 4: collect DD type → ask Step 5 (Raw Spread)
async def _wiz_max_dd_daily(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()

    if _wizard_data.get("_dd_type_confirming"):
        if v.upper() == "CONFIRM":
            _wizard_data["drawdown_is_static"] = False
            _wizard_data.pop("_dd_type_confirming")
            await update.message.reply_text(
                "<b>Step 5/10 — Raw Spread Account</b>\n"
                "Type: <code>yes</code> or <code>no</code>",
                parse_mode="HTML",
            )
            return PF_DD_TYPE
        else:
            _wizard_data.pop("_dd_type_confirming")
            await update.message.reply_text(
                "⚠️ <b>Confirmation Not Received</b>\n\n"
                "Re-enter drawdown type:\n"
                "<code>static</code> or <code>dynamic</code>",
                parse_mode="HTML",
            )
            return PF_MAX_DD_DAILY

    v_lower = v.lower()
    if v_lower == "static":
        _wizard_data["drawdown_is_static"] = True
        await update.message.reply_text(
            "<b>Step 5/10 — Raw Spread Account</b>\n"
            "Type: <code>yes</code> or <code>no</code>",
            parse_mode="HTML",
        )
        return PF_DD_TYPE
    elif v_lower == "dynamic":
        _wizard_data["_dd_type_confirming"] = True
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Dynamic Drawdown Flagged</b>')}"
            "This system is designed mainly for static drawdown accounts.\n\n"
            "Reply <b>CONFIRM</b> to continue with dynamic,\n"
            "or type <code>static</code> to correct.",
            parse_mode="HTML",
        )
        return PF_MAX_DD_DAILY
    else:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nType: <code>static</code> or <code>dynamic</code>",
            parse_mode="HTML",
        )
        return PF_MAX_DD_DAILY


# Step 5: collect raw spread → ask Step 6 (Profit Share)
async def _wiz_dd_type(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()

    if _wizard_data.get("_raw_spread_confirming"):
        if v.upper() == "CONFIRM":
            _wizard_data["raw_spread_account"] = False
            _wizard_data.pop("_raw_spread_confirming")
            await update.message.reply_text(
                "<b>Step 6/10 — Profit Sharing</b>\n"
                "Enter the profit sharing percentage.\n\n"
                "Example: <code>80</code>",
                parse_mode="HTML",
            )
            return PF_RAW_SPREAD
        else:
            _wizard_data.pop("_raw_spread_confirming")
            await update.message.reply_text(
                "⚠️ <b>Confirmation Not Received</b>\n\n"
                "Re-enter: <code>yes</code> or <code>no</code>",
                parse_mode="HTML",
            )
            return PF_DD_TYPE

    v_lower = v.lower()
    if v_lower == "yes":
        _wizard_data["raw_spread_account"] = True
        await update.message.reply_text(
            "<b>Step 6/10 — Profit Sharing</b>\n"
            "Enter the profit sharing percentage.\n\n"
            "Example: <code>80</code>",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD
    elif v_lower == "no":
        _wizard_data["_raw_spread_confirming"] = True
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Non-Raw Spread Flagged</b>')}"
            "This system is designed mainly for raw spread accounts.\n\n"
            "Reply <b>CONFIRM</b> to continue,\n"
            "or type <code>yes</code> to correct.",
            parse_mode="HTML",
        )
        return PF_DD_TYPE
    else:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nType: <code>yes</code> or <code>no</code>",
            parse_mode="HTML",
        )
        return PF_DD_TYPE


# Step 6: collect profit share → ask Step 7 (Min Days)
async def _wiz_raw_spread(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert 0 < v <= 100
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a number between 1 and 100.",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD
    _wizard_data["profit_sharing_pct"] = v
    await update.message.reply_text(
        "<b>Step 7/10 — Minimum Profit Days</b>\n"
        "Enter the minimum trading days required.\n\n"
        "Example: <code>5</code>",
        parse_mode="HTML",
    )
    return PF_PROFIT_SHARE


# Step 7: collect min days → ask Step 8 (Consistency)
async def _wiz_profit_share(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = int(update.message.text.strip())
        assert v >= 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a whole number.\nExample: <code>5</code>",
            parse_mode="HTML",
        )
        return PF_PROFIT_SHARE
    _wizard_data["min_profit_days"] = v
    await update.message.reply_text(
        "<b>Step 8/10 — Consistency Rule</b>\n"
        "Enter the consistency rule percentage.\n\n"
        "Example: <code>30</code>\n\n"
        "The bot will apply the configured buffer automatically.",
        parse_mode="HTML",
    )
    return PF_MIN_DAYS


# Step 8: collect consistency → ask Step 9 (Prop Baseline)
async def _wiz_min_days(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert 2.0 <= v <= 50.0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a number between 2 and 50.\nExample: <code>30</code>",
            parse_mode="HTML",
        )
        return PF_MIN_DAYS
    _wizard_data["consistency_threshold_pct"] = v
    await update.message.reply_text(
        "<b>Step 9/10 — Prop Baseline</b>\n"
        "Enter the prop account starting balance.\n\n"
        "This is used as the Prop baseline.\n\n"
        "Example: <code>100000</code>",
        parse_mode="HTML",
    )
    return PF_CONSISTENCY


# Step 9: collect prop baseline → ask Step 10 (Personal Baseline)
async def _wiz_consistency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip().replace(",", ""))
        assert v > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>100000</code>",
            parse_mode="HTML",
        )
        return PF_CONSISTENCY
    _wizard_data["prop_baseline"] = v
    await update.message.reply_text(
        "<b>Step 10/10 — Personal Baseline</b>\n"
        "Enter the personal account starting balance.\n\n"
        "This is used as the Personal baseline.\n\n"
        "Example: <code>10000</code>",
        parse_mode="HTML",
    )
    return PF_INITIAL_BALANCE


# Step 10: collect personal baseline → show review → PF_CONFIRM
async def _wiz_initial_balance(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip().replace(",", ""))
        assert v > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>10000</code>",
            parse_mode="HTML",
        )
        return PF_INITIAL_BALANCE
    _wizard_data["pers_baseline"] = v

    prop_b = _wizard_data["prop_baseline"]
    eff = _apply_buffers(_wizard_data)
    dd_flag    = "  <b>[FLAGGED]</b>" if not _wizard_data["drawdown_is_static"] else ""
    rs_flag    = "  <b>[FLAGGED]</b>" if not _wizard_data["raw_spread_account"] else ""
    daily_dd_raw = _wizard_data["max_drawdown_daily_pct"]
    daily_dd_eff = eff["max_drawdown_daily_pct"]
    cons_raw   = _wizard_data["consistency_threshold_pct"]
    cons_eff   = eff["consistency_threshold_pct"]
    daily_dd_amt = round(prop_b * daily_dd_eff / 100.0, 2)
    floor_amt    = round(prop_b * (1.0 - eff["max_drawdown_overall_pct"] / 100.0), 2)
    cap_amt      = round(prop_b * eff["daily_profit_cap_pct"] / 100.0, 2)
    target_lvl   = round(prop_b * (1.0 + _wizard_data["profit_target_pct"] / 100.0), 2)
    pers_ccy = await _pers_currency()   # personal baseline shown in account currency (SGD), never $

    summary = (
        f"{_cmd_header('📊 <b>Review Account Setup</b>')}"
        f"<b>Prop Rules</b>\n"
        f"Profit target: {_wizard_data['profit_target_pct']:.1f}%\n"
        f"Overall DD: {_wizard_data['max_drawdown_overall_pct']:.1f}%\n"
        f"Daily DD: {daily_dd_raw:.1f}% → {daily_dd_eff:.1f}%\n"
        f"Consistency: {cons_raw:.1f}% → {cons_eff:.1f}%\n\n"
        f"<b>Baselines</b>\n"
        f"Prop: ${prop_b:,.2f}\n"
        f"Personal: {_money(v, pers_ccy)}\n\n"
        f"<b>Account Type</b>\n"
        f"Drawdown: {'Static' if _wizard_data['drawdown_is_static'] else 'Dynamic'}{dd_flag}\n"
        f"Raw spread: {'Yes' if _wizard_data['raw_spread_account'] else 'No'}{rs_flag}\n"
        f"Profit sharing: {_wizard_data['profit_sharing_pct']:.1f}%\n"
        f"Min profit days: {_wizard_data['min_profit_days']}\n\n"
        f"<b>Kill Levels</b>\n"
        f"K1 Daily DD: −${daily_dd_amt:,.2f}\n"
        f"K2 Overall floor: ${floor_amt:,.2f}\n"
        f"K3 Daily cap: +${cap_amt:,.2f}\n"
        f"K4 Profit target: ${target_lvl:,.2f}\n\n"
        f"Reply <b>YES</b> to save, or <b>NO</b> to cancel."
    )
    await update.message.reply_text(summary, parse_mode="HTML")
    return PF_CONFIRM


async def _wiz_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip().upper()
    if v == "NO":
        _wizard_data.clear()
        await update.message.reply_text(
            f"{_cmd_header('🟡 <b>Cancelled</b>')}No changes were saved.",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    if v != "YES":
        await update.message.reply_text(
            "Reply <b>YES</b> to save, or <b>NO</b> to cancel.",
            parse_mode="HTML",
        )
        return PF_CONFIRM

    eff = _apply_buffers(_wizard_data)

    # Prop baseline = user-entered in Step 9 — static for the evaluation life.
    # Personal baseline = user-entered in Step 10 — manual only, never auto-set.
    baseline      = _wizard_data.get("prop_baseline", 0.0)
    pers_baseline = _wizard_data.get("pers_baseline", 0.0)
    day_start     = baseline
    try:
        day_start = _query_equity(ZMQ_REQ_PROP, "")["balance"]
    except Exception:
        pass  # fall back to baseline if MT5 unavailable

    with _pf_lock:
        _propfirm.update({
            "propfirm_name":              "Prop Account",
            "profit_target_pct":          _wizard_data["profit_target_pct"],
            "max_drawdown_overall_pct":   eff["max_drawdown_overall_pct"],
            "max_drawdown_daily_pct":     eff["max_drawdown_daily_pct"],
            "drawdown_is_static":         _wizard_data["drawdown_is_static"],
            "raw_spread_account":         _wizard_data["raw_spread_account"],
            "profit_sharing_pct":         _wizard_data["profit_sharing_pct"],
            "min_profit_days":            _wizard_data["min_profit_days"],
            "daily_profit_cap_pct":       eff["daily_profit_cap_pct"],
            "consistency_threshold_pct":  eff["consistency_threshold_pct"],
            "baseline_equity":            baseline,
            "pers_baseline_equity":       pers_baseline,
            "day_start_equity":           day_start,
            "day_start_date_utc":         _propfirm_day(_sgt_now()),
            "k1_layer":                   0,
            "k3_layer":                   0,
        })
        # Store raw Phase 1 values for /phase2 wizard
        _propfirm.setdefault("phase_configs", {})["1"] = {
            "profit_target_pct":          _wizard_data["profit_target_pct"],
            "max_drawdown_overall_pct":   _wizard_data["max_drawdown_overall_pct"],
            "max_drawdown_daily_pct":     _wizard_data["max_drawdown_daily_pct"],
            "drawdown_is_static":         _wizard_data["drawdown_is_static"],
            "raw_spread_account":         _wizard_data["raw_spread_account"],
            "profit_sharing_pct":         _wizard_data["profit_sharing_pct"],
            "min_profit_days":            _wizard_data["min_profit_days"],
            "consistency_threshold_pct":  _wizard_data["consistency_threshold_pct"],
        }
        _save_propfirm(_propfirm)

    if baseline > 0:
        _dispatch_parameters()

    # New prop-firm = new cycle → restart the per-cycle trading-fee counter.
    await asyncio.to_thread(_dispatch_fee_anchor_reset)

    dd_daily   = eff["max_drawdown_daily_pct"]
    dd_overall = eff["max_drawdown_overall_pct"]
    cap        = eff["daily_profit_cap_pct"]
    target_pct = _wizard_data["profit_target_pct"]
    daily_dd_amt = round(baseline * dd_daily   / 100.0, 2) if baseline > 0 else 0.0
    cap_amt      = round(baseline * cap        / 100.0, 2) if baseline > 0 else 0.0
    overall_fl   = round(baseline * (1 - dd_overall / 100.0), 2) if baseline > 0 else 0.0
    target_lvl   = round(baseline * (1.0 + target_pct / 100.0), 2) if baseline > 0 else 0.0

    _wizard_data.clear()
    pers_ccy = await _pers_currency()   # personal baseline in account currency (SGD), never $
    await update.message.reply_text(
        f"{_cmd_header('✅ <b>Account Setup Saved</b>')}"
        f"<b>Baselines</b>\n"
        f"Prop: ${baseline:,.2f}\n"
        f"Personal: {_money(pers_baseline, pers_ccy)}\n\n"
        f"<b>Risk Levels</b>\n"
        f"K1 Daily DD: −${daily_dd_amt:,.2f}\n"
        f"K2 Overall floor: ${overall_fl:,.2f}\n"
        f"K3 Daily cap: +${cap_amt:,.2f}\n"
        f"K4 Profit target: ${target_lvl:,.2f}\n\n"
        f"<b>Next Step</b>\n/phase1 or /phase2",
        parse_mode="HTML",
    )
    logger.info("Account setup saved — prop_baseline=%.2f  pers_baseline=%.2f",
                baseline, pers_baseline)
    return ConversationHandler.END


async def _wiz_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text(
        f"{_cmd_header('🟡 <b>Cancelled</b>')}No changes were saved.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ── /changepropfirm wizard — /back handlers (one per step) ───────────────

async def _wiz_back_step1(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 1/10 — Profit Target</b>\nEnter the profit target percentage.\n\nExample: <code>10</code>",
        parse_mode="HTML")
    return PF_NAME

async def _wiz_back_step2(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 1/10 — Profit Target</b>\nEnter the profit target percentage.\n\nExample: <code>10</code>",
        parse_mode="HTML")
    return PF_NAME

async def _wiz_back_step3(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 2/10 — Overall Drawdown</b>\nEnter the overall drawdown limit.\n\nExample: <code>10</code>",
        parse_mode="HTML")
    return PF_PROFIT_TARGET

async def _wiz_back_step4(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 3/10 — Daily Drawdown</b>\nEnter the daily drawdown limit.\n\nExample: <code>3</code>\n\n"
        "The bot will apply the configured buffer automatically.",
        parse_mode="HTML")
    return PF_MAX_DD_OVERALL

async def _wiz_back_step5(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _wizard_data.pop("_dd_type_confirming", None)
    await update.message.reply_text(
        "<b>Step 4/10 — Drawdown Type</b>\nType: <code>static</code> or <code>dynamic</code>",
        parse_mode="HTML")
    return PF_MAX_DD_DAILY

async def _wiz_back_step6(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _wizard_data.pop("_raw_spread_confirming", None)
    await update.message.reply_text(
        "<b>Step 5/10 — Raw Spread Account</b>\nType: <code>yes</code> or <code>no</code>",
        parse_mode="HTML")
    return PF_DD_TYPE

async def _wiz_back_step7(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 6/10 — Profit Sharing</b>\nEnter the profit sharing percentage.\n\nExample: <code>80</code>",
        parse_mode="HTML")
    return PF_RAW_SPREAD

async def _wiz_back_step8(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 7/10 — Minimum Profit Days</b>\nEnter the minimum trading days required.\n\nExample: <code>5</code>",
        parse_mode="HTML")
    return PF_PROFIT_SHARE

async def _wiz_back_step9(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 8/10 — Consistency Rule</b>\nEnter the consistency rule percentage.\n\n"
        "Example: <code>30</code>\n\nThe bot will apply the configured buffer automatically.",
        parse_mode="HTML")
    return PF_MIN_DAYS

async def _wiz_back_step10(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 9/10 — Prop Baseline</b>\nEnter the prop account starting balance.\n\n"
        "This is used as the Prop baseline.\n\nExample: <code>100000</code>",
        parse_mode="HTML")
    return PF_CONSISTENCY

async def _wiz_back_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "<b>Step 10/10 — Personal Baseline</b>\nEnter the personal account starting balance.\n\n"
        "This is used as the Personal baseline.\n\nExample: <code>10000</code>",
        parse_mode="HTML")
    return PF_INITIAL_BALANCE


# ── Telegram commands ─────────────────────────────────────────────────────


async def _cmd_phase1(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text(
        "⚙️ <b>Phase 1 Setup</b>\n\n"
        "Send first-trade  <code>reward:risk</code>  (in $)\n"
        "   e.g.  <code>9000:2000</code>\n\n"
        "• <b>Reward</b> — profit target of your FIRST Phase 1 "
        "trade (sets Stage 1 = baseline + this).\n"
        "• <b>Risk</b> — fixed $ lost if any trade hits SL. "
        "Identical for every trade.\n\n"
        "ℹ️ Remaining stages are spread automatically:\n"
        "   (overall target − first reward) ÷ (min profitable days − 1)\n\n"
        "/cancel to abort.",
        parse_mode="HTML",
    )
    return P1_INPUT


async def _p1_input(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        first_reward, fixed_risk = phase1_strategy.parse_reward_risk(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(
            f"⚠️ <b>Invalid Input</b>\n\n{exc}\n\n"
            f"Format: <code>reward:risk</code> e.g. <code>9000:2000</code>",
            parse_mode="HTML",
        )
        return P1_INPUT

    with _pf_lock:
        pf = dict(_propfirm)
    baseline       = pf.get("baseline_equity", 0.0)
    target_pct     = pf.get("profit_target_pct", 0.0)
    min_days       = int(pf.get("min_profit_days", 0))
    overall_dd_pct = pf.get("max_drawdown_overall_pct", 0.0)

    if baseline <= 0:
        balance, err = await asyncio.to_thread(_lock_baseline_from_live)
        if err:
            await update.message.reply_text(
                f"{_cmd_header('⚠️ <b>Baseline Missing</b>')}"
                f"Could not set baseline: <code>{err}</code>\n\n"
                f"Run /changepropfirm first, then /phase1 again.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        baseline = balance

    verr = phase1_strategy.validate_phase1_inputs(
        first_reward, fixed_risk, baseline, target_pct, min_days)
    if verr:
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Cannot Configure Phase 1</b>')}{verr}",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    stages = phase1_strategy.derive_stages(baseline, first_reward, target_pct, min_days)
    target_amt  = baseline * target_pct / 100.0
    overall_amt = baseline * overall_dd_pct / 100.0
    _wizard_data["p1"] = {
        "first_reward": first_reward, "fixed_risk": fixed_risk,
        "stages": stages, "baseline": baseline,
    }
    stage_str = "  →  ".join(f"${s:,.0f}" for s in stages)
    warn = ""
    daily_room = baseline * pf.get("max_drawdown_daily_pct", 0.0) / 100.0
    if daily_room > 0 and fixed_risk >= daily_room:
        warn = (f"\n\n⚠️ Risk ${fixed_risk:,.0f} ≥ daily-DD room ${daily_room:,.0f} "
                f"— only one losing trade fits per day.")

    await update.message.reply_text(
        f"{_cmd_header('✅ <b>Phase 1 Ready</b>')}"
        f"First reward : ${first_reward:,.0f}  → Stage 1 = ${stages[0]:,.0f}\n"
        f"Fixed risk   : ${fixed_risk:,.0f}   (every trade)\n"
        f"Stages       : {stage_str}\n"
        f"Overall stop / target : ${baseline - overall_amt:,.0f} / ${baseline + target_amt:,.0f}"
        f"{warn}\n\n"
        f"Reply <code>CONFIRM</code> to proceed.\n"
        f"Send /cancel to abort.",
        parse_mode="HTML",
    )
    return P1_CONFIRM


async def _p1_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if (update.message.text or "").strip() != "CONFIRM":
        await update.message.reply_text(
            "⚠️ <b>Confirmation Required</b>\n\nType <code>CONFIRM</code> to proceed, or /cancel to abort.",
            parse_mode="HTML",
        )
        return P1_CONFIRM

    d = _wizard_data.get("p1")
    if not d:
        await update.message.reply_text("⚠️ Session expired. Run /phase1 again.", parse_mode="HTML")
        return ConversationHandler.END

    with _state_lock:
        _phase_state["phase"] = 1
        _phase_state.pop("permanently_halted", None)
        _phase_state.pop("phase1_permanently_halted", None)
        _save_phase(_phase_state)

    _phase1_init(d["first_reward"], d["fixed_risk"], d["stages"])
    await asyncio.to_thread(_dispatch_parameters)

    # Phase 1 (re)start = fresh cycle → restart the per-cycle trading-fee counter
    # on BOTH workers (same as /changepropfirm and /phase2).
    await asyncio.to_thread(_dispatch_fee_anchor_reset)
    _wizard_data.clear()

    stage_str = "  →  ".join(f"${s:,.0f}" for s in d["stages"])
    await update.message.reply_text(
        f"{_cmd_header('🟢 <b>Phase 1 Active</b>')}"
        f"Personal multiplier: ×{PHASE_MULT[1]:.2f}\n"
        f"Prop baseline: ${d['baseline']:,.2f}\n"
        f"Fixed risk: ${d['fixed_risk']:,.0f} / trade\n"
        f"Stages: {stage_str}\n\n"
        f"<b>Next Step</b>\n/resume",
        parse_mode="HTML",
    )
    logger.info("Telegram: phase 1 configured  reward=%.2f risk=%.2f stages=%s",
                d["first_reward"], d["fixed_risk"], d["stages"])
    return ConversationHandler.END


async def _p1_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _wizard_data.clear()
    await update.message.reply_text("❌ Phase 1 setup cancelled.", parse_mode="HTML")
    return ConversationHandler.END


# ── Phase 2 setup wizard (/phase2) ───────────────────────────────────────

async def _cmd_phase2(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END

    with _pf_lock:
        phase1_cfg = _propfirm.get("phase_configs", {}).get("1")

    if not phase1_cfg:
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Phase 1 Config Missing</b>')}"
            "Run /changepropfirm first to configure Phase 1 settings.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    _p2_wizard_data.clear()
    _p2_wizard_data["phase1_config"] = dict(phase1_cfg)
    new_cfg = dict(phase1_cfg)
    _p2_wizard_data["new_config"] = new_cfg

    block = _p2_settings_block(phase1_cfg)
    await update.message.reply_text(
        f"{_cmd_header('🟢 <b>Phase 2 Setup</b>')}"
        f"<b>Phase 1 Settings</b>\n{block}\n\n"
        f"Use the same details for Phase 2? Reply <b>yes</b> or <b>no</b>.",
        parse_mode="HTML",
    )
    return P2_SAME_OR_DIFF


async def _p2_same_or_diff(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip().lower()
    if v == "yes":
        _p2_wizard_data["fields_to_change"] = []
        return await _p2_show_review(update)
    if v == "no":
        block = _p2_settings_block(_p2_wizard_data["phase1_config"])
        await update.message.reply_text(
            f"<b>Current Settings</b>\n{block}\n\n"
            f"<b>Which settings should change?</b>\n"
            f"Reply with numbers separated by spaces.\n"
            f"Example: <code>2 4</code> | Range: 1–8",
            parse_mode="HTML",
        )
        return P2_WHICH_FIELDS
    await update.message.reply_text("⚠️ <b>Invalid Reply</b>\n\nReply <b>yes</b> or <b>no</b>.", parse_mode="HTML")
    return P2_SAME_OR_DIFF


async def _p2_which_fields(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    indices = []
    for t in update.message.text.strip().split():
        try:
            n = int(t)
            if 1 <= n <= 8 and n not in indices:
                indices.append(n)
        except ValueError:
            pass
    if not indices:
        await update.message.reply_text(
            "⚠️ <b>No Valid Settings Selected</b>\n\n"
            "Enter numbers 1–8 separated by spaces.\n"
            "Example: <code>2 4</code>",
            parse_mode="HTML",
        )
        return P2_WHICH_FIELDS
    _p2_wizard_data["fields_to_change"] = sorted(indices)
    _p2_wizard_data["field_iter_idx"]   = 0
    return await _p2_ask_current_field(update)


async def _p2_ask_current_field(update) -> int:
    idx    = _p2_wizard_data["field_iter_idx"]
    fields = _p2_wizard_data["fields_to_change"]
    if idx >= len(fields):
        return await _p2_show_review(update)
    _, key, name, input_type = _P2_FIELD_BY_IDX[fields[idx]]
    current = _p2_wizard_data["phase1_config"].get(key, "—")
    hints   = {
        "str":            "Enter the new value:",
        "float_pos":      f"Enter a positive number (e.g. <code>10</code>):",
        "static_dynamic": "Type <code>static</code> or <code>dynamic</code>:",
        "yes_no":         "Type <code>yes</code> or <code>no</code>:",
        "int_nn":         "Enter a whole number ≥ 0:",
    }
    await update.message.reply_text(
        f"<b>Update Setting {fields[idx]} — {name}</b>\n\n"
        f"Current Phase 1 value: {_p2_display(key, current)}\n\n"
        f"{hints[input_type]}",
        parse_mode="HTML",
    )
    return P2_COLLECTING


async def _p2_collect_field(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    idx      = _p2_wizard_data["field_iter_idx"]
    fields   = _p2_wizard_data["fields_to_change"]
    _, key, name, input_type = _P2_FIELD_BY_IDX[fields[idx]]
    text  = update.message.text.strip()
    value = None
    error = None
    if input_type == "str":
        value = text
    elif input_type == "float_pos":
        try:
            value = float(text)
            assert value > 0
        except Exception:
            error = "Enter a positive number (e.g. <code>10</code>):"
    elif input_type == "static_dynamic":
        if text.lower() == "static":
            value = True
        elif text.lower() == "dynamic":
            value = False
        else:
            error = "Type <code>static</code> or <code>dynamic</code>:"
    elif input_type == "yes_no":
        if text.lower() == "yes":
            value = True
        elif text.lower() == "no":
            value = False
        else:
            error = "Type <code>yes</code> or <code>no</code>:"
    elif input_type == "int_nn":
        try:
            value = int(text)
            assert value >= 0
        except Exception:
            error = "Enter a whole number ≥ 0:"
    if error:
        await update.message.reply_text(error, parse_mode="HTML")
        return P2_COLLECTING
    _p2_wizard_data["new_config"][key]  = value
    _p2_wizard_data["field_iter_idx"]   = idx + 1
    return await _p2_ask_current_field(update)


async def _p2_show_review(update) -> int:
    new = _p2_wizard_data["new_config"]
    p1  = _p2_wizard_data["phase1_config"]
    eff = _apply_buffers(new)
    block   = _p2_settings_block(new, compare_to=p1)
    dd_flag = "  <b>[FLAGGED]</b>" if not new.get("drawdown_is_static") else ""
    rs_flag = "  <b>[FLAGGED]</b>" if not new.get("raw_spread_account") else ""
    await update.message.reply_text(
        f"{_cmd_header('📊 <b>Phase 2 Review</b>')}"
        f"{block}\n\n"
        f"Drawdown: {_p2_display('drawdown_is_static', new['drawdown_is_static'])}{dd_flag}\n"
        f"Raw spread: {_p2_display('raw_spread_account', new['raw_spread_account'])}{rs_flag}\n\n"
        f"Reply <b>YES</b> to proceed, or <b>NO</b> to cancel.",
        parse_mode="HTML",
    )
    return P2_INITIAL_BALANCE


async def _p2_initial_balance(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip().upper()
    if v == "NO":
        _p2_wizard_data.clear()
        await update.message.reply_text(f"{_cmd_header('🟡 <b>Cancelled</b>')}No changes were saved.", parse_mode="HTML")
        return ConversationHandler.END
    if v != "YES":
        await update.message.reply_text("Reply <b>YES</b> to proceed, or <b>NO</b> to cancel.", parse_mode="HTML")
        return P2_INITIAL_BALANCE
    await update.message.reply_text(
        "<b>Prop Baseline</b>\n"
        "Enter the prop account starting balance for this Phase 2 cycle.\n\n"
        "Example: <code>200000</code>",
        parse_mode="HTML",
    )
    return P2_PERS_BALANCE


async def _p2_pers_balance(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v_prop = float(update.message.text.strip().replace(",", ""))
        assert v_prop > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>200000</code>",
            parse_mode="HTML",
        )
        return P2_PERS_BALANCE
    _p2_wizard_data["prop_baseline"] = v_prop
    await update.message.reply_text(
        "<b>Personal Baseline</b>\n"
        "Enter the personal account starting balance.\n\n"
        "Example: <code>10000</code>",
        parse_mode="HTML",
    )
    return P2_CONFIRM


async def _p2_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v_pers = float(update.message.text.strip().replace(",", ""))
        assert v_pers > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>10000</code>",
            parse_mode="HTML",
        )
        return P2_CONFIRM

    new           = _p2_wizard_data["new_config"]
    eff           = _apply_buffers(new)
    baseline      = _p2_wizard_data["prop_baseline"]
    pers_baseline = v_pers

    day_start = baseline
    try:
        day_start = _query_equity(ZMQ_REQ_PROP, "")["balance"]
    except Exception:
        pass

    today = _propfirm_day(_sgt_now())
    cons_threshold = eff["consistency_threshold_pct"]
    with _pf_lock:
        _propfirm.update({
            "propfirm_name":              "Prop Account",
            "profit_target_pct":          new["profit_target_pct"],
            "max_drawdown_overall_pct":   eff["max_drawdown_overall_pct"],
            "max_drawdown_daily_pct":     eff["max_drawdown_daily_pct"],
            "drawdown_is_static":         new["drawdown_is_static"],
            "raw_spread_account":         new["raw_spread_account"],
            "profit_sharing_pct":         new["profit_sharing_pct"],
            "min_profit_days":            new["min_profit_days"],
            "daily_profit_cap_pct":       eff["daily_profit_cap_pct"],
            "consistency_threshold_pct":  cons_threshold,
            "baseline_equity":            baseline,
            "pers_baseline_equity":       pers_baseline,
            "day_start_equity":           day_start,
            "day_start_date_utc":         today,
            "k1_layer":                   0,
            "k3_layer":                   0,
        })
        _propfirm.setdefault("phase_configs", {})["2"] = {k: new[k] for k in new}
        _save_propfirm(_propfirm)

    _reset_consistency_log()

    with _state_lock:
        _phase_state["phase"] = 2
        _phase_state.pop("permanently_halted", None)
        _phase_state.pop("phase1_permanently_halted", None)
        _save_phase(_phase_state)

    if baseline > 0:
        _dispatch_parameters()

    # Phase 2 = fresh cycle → restart the per-cycle trading-fee counter.
    await asyncio.to_thread(_dispatch_fee_anchor_reset)

    floor_amt    = round(baseline * (1.0 - eff["max_drawdown_overall_pct"] / 100.0), 2) if baseline > 0 else 0.0
    daily_dd_amt = round(baseline * eff["max_drawdown_daily_pct"]  / 100.0, 2) if baseline > 0 else 0.0
    cap_amt      = round(baseline * eff["daily_profit_cap_pct"]    / 100.0, 2) if baseline > 0 else 0.0
    target_lvl   = round(baseline * (1.0 + new["profit_target_pct"] / 100.0), 2) if baseline > 0 else 0.0

    _p2_wizard_data.clear()
    pers_ccy = await _pers_currency()   # personal baseline in account currency (SGD), never $
    await update.message.reply_text(
        f"{_cmd_header('🟢 <b>Phase 2 Active</b>')}"
        f"<b>Risk Mode</b>\n"
        f"Personal multiplier: ×{PHASE_MULT[2]:.2f}\n\n"
        f"<b>Baselines</b>\n"
        f"Prop: ${baseline:,.2f}\n"
        f"Personal: {_money(pers_baseline, pers_ccy)}\n\n"
        f"<b>Risk Levels</b>\n"
        f"K1 Daily DD: −${daily_dd_amt:,.2f}\n"
        f"K2 Overall floor: ${floor_amt:,.2f}\n"
        f"K3 Daily cap: +${cap_amt:,.2f}\n"
        f"K4 Profit target: ${target_lvl:,.2f}\n\n"
        f"<b>Next Step</b>\n/resume",
        parse_mode="HTML",
    )
    logger.info("Phase 2 started — prop_baseline=%.2f  pers_baseline=%.2f", baseline, pers_baseline)
    return ConversationHandler.END


async def _p2_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _p2_wizard_data.clear()
    await update.message.reply_text(
        f"{_cmd_header('🟡 <b>Cancelled</b>')}No changes were saved.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    with _state_lock:
        _phase_state["active"] = False
        _save_phase(_phase_state)

    try:
        prop_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
    except Exception:
        prop_pos = []
    try:
        pers_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS)
    except Exception:
        pers_pos = []

    pers_block = _cmd_pos_block("Personal Signal", pers_pos, None, detail=False, show_pnl=False)
    prop_block = _cmd_pos_block("Prop Hedge", prop_pos, None, detail=False, show_pnl=False)
    body = (
        "New signals will be ignored.\n"
        "Existing open trades remain active unless closed manually.\n\n"
        "<b>Open Positions</b>\n\n"
        f"{pers_block}\n\n{prop_block}\n\n"
        "<b>Next Step</b>\n"
        "/resume — re-enable signals\n"
        "/emergency — force-close all positions"
    )
    await update.message.reply_text(
        f"{_cmd_header('🟡 <b>Signal Processing Halted</b>')}{body}",
        parse_mode="HTML",
    )
    logger.warning("Telegram: halted by user")


async def _cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        p_halt = _phase_state.get("permanently_halted", False)
    if p_halt:
        await update.message.reply_text(
            f"{_cmd_header('🔴 <b>Resume Blocked</b>')}"
            "Profit target has already been reached.\n\n"
            "<b>Next Step</b>\nSend /phase2 to configure and start the next phase.",
            parse_mode="HTML",
        )
        return
    now_sgt = _sgt_now()
    today_day = _propfirm_day(now_sgt)
    with _state_lock:
        had_daily_halt = _phase_state.get("daily_halted", False)
        _phase_state["active"] = True
        _phase_state.pop("daily_halted", None)
        _phase_state.pop("daily_halted_date", None)
        # User /resume is a manual override of today's soft kills (K1/K3, Phase 1
        # stage_reached). Without this, the monitor's next tick would immediately
        # re-fire whichever kill is still tripped (e.g. K3 cap still breached) and
        # silently undo this resume. Permanent kills (K2/K4/K5) are NOT suppressed
        # — they still execute. The override naturally expires at the next
        # prop-firm day rollover (11:00 SGT).
        _phase_state["soft_kill_override_day"] = today_day
        _save_phase(_phase_state)

    try:
        prop_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
    except Exception:
        prop_pos = []
    try:
        pers_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS)
    except Exception:
        pers_pos = []

    curfew_note = "\n\n<i>Trading window is currently closed. Signals resume when the window opens.</i>" if _is_sgt_curfew() else ""
    override_note = (
        "\n\n<i>Today's daily-loss / profit-cap kills are suppressed until the next session — manual override active.</i>"
        if had_daily_halt else ""
    )
    pers_block = _cmd_pos_block("Personal Signal", pers_pos, None, detail=False, show_pnl=False)
    prop_block = _cmd_pos_block("Prop Hedge", prop_pos, None, detail=False, show_pnl=False)
    body = (
        "New signals are now allowed.\n\n"
        "<b>Open Positions</b>\n\n"
        f"{pers_block}\n\n{prop_block}"
        f"{override_note}{curfew_note}"
    )
    await update.message.reply_text(
        f"{_cmd_header('🟢 <b>Signal Processing Resumed</b>')}{body}",
        parse_mode="HTML",
    )
    logger.info("Telegram: resumed by user — soft_kill_override_day=%s", today_day)


async def _cmd_rearm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-arm today's SOFT kills after an accidental /resume.

    /resume sets a same-day override that suppresses K1 (daily DD), K3 (daily
    profit cap) and the Phase 1 stage halt for the rest of the session — so if
    you /resume by mistake, today's daily halt stops working. This clears that
    override so the monitor will halt again the moment a daily kill is tripped.
    Permanent kills (K2/K4/K5) are never suppressed and are unaffected."""
    if not _auth(update):
        return
    with _state_lock:
        had_override = bool(_phase_state.get("soft_kill_override_day", ""))
        _phase_state.pop("soft_kill_override_day", None)
        _save_phase(_phase_state)
    if had_override:
        body = (
            "Today's daily-loss (K1) and profit-cap (K3) kills — plus the Phase 1 "
            "stage halt — are <b>active again</b>. If one is already tripped, the next "
            "monitor tick will halt trading.\n\n"
            "<i>Permanent kills (K2/K4/K5) were never suppressed.</i>"
        )
    else:
        body = (
            "No soft-kill override was active — nothing to re-arm.\n\n"
            "Today's K1/K3 are already armed."
        )
    await update.message.reply_text(
        f"{_cmd_header('🔁 <b>Soft Kills Re-armed</b>')}{body}",
        parse_mode="HTML",
    )
    logger.info("Telegram: soft-kill override cleared by /rearm (had_override=%s)", had_override)


async def _cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        phase    = _phase_state.get("phase", "?")
        active   = _phase_state.get("active", False)
        last_ts  = _phase_state.get("last_signal_ts", "never")
        p_halt   = _phase_state.get("permanently_halted", False)
        max_pos  = _phase_state.get("max_open_positions", 2)
        ovr_day  = _phase_state.get("soft_kill_override_day", "")
    soft_override_active = bool(ovr_day) and ovr_day == _propfirm_day(_sgt_now())
    with _manual_suppress_lock:
        manual_blocks = len(_manual_suppressed_pairs)
    with _news_suppressed_lock:
        now_utc    = datetime.now(timezone.utc)
        news_blocks = sum(1 for end in _news_suppressed_pairs.values() if end > now_utc)
    mult   = PHASE_MULT.get(phase, "?")
    curfew = _is_sgt_curfew()
    with _window_lock:
        win_curr = dict(_trading_window["current_window"])
        win_next = _trading_window.get("next_window")
    if last_ts and last_ts != "never":
        try:
            _sgt_off = timedelta(hours=8)
            last_ts_display = (datetime.fromisoformat(last_ts) + _sgt_off).strftime("%Y-%m-%d %H:%M SGT")
        except Exception:
            last_ts_display = last_ts
    else:
        last_ts_display = "never"

    win_line = f"{win_curr['start']}–{win_curr['end']} SGT"
    if win_next:
        win_line += f" | Next: {win_next['start']}–{win_next['end']}"

    override_line = (
        f"Soft-kill override: 🟠 Active (K1/K3 suppressed until next session)\n"
        if soft_override_active else ""
    )

    await update.message.reply_text(
        f"{_cmd_header('📊 <b>System Status</b>')}"
        f"<b>Trading</b>\n"
        f"Phase: {phase} (×{mult})\n"
        f"Status: {'🟢 Active' if active else '🟡 Halted'}\n"
        f"Permanent halt: {'Yes' if p_halt else 'No'}\n"
        f"Trading window: {win_line}\n"
        f"Curfew: {'Yes — dormant' if curfew else 'No'}\n"
        f"{override_line}"
        f"\n<b>Risk Control</b>\n"
        f"Max positions: {max_pos}\n"
        f"Manual blocks: {manual_blocks}\n"
        f"News blocks: {news_blocks}\n\n"
        f"<b>Activity</b>\n"
        f"Last signal: {last_ts_display}",
        parse_mode="HTML",
    )


async def _cmd_propfirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _pf_lock:
        pf = dict(_propfirm)
    prop_b = pf.get("baseline_equity", 0.0)
    pers_b = pf.get("pers_baseline_equity", 0.0)
    # Best-effort personal account currency so an SGD baseline isn't mislabelled $.
    try:
        _pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_ccy = _pers.get("account_currency", "USD")
    except Exception:
        pers_ccy = "USD"
    await update.message.reply_text(
        f"{_cmd_header('📊 <b>Prop Account Rules</b>')}"
        f"<b>Targets</b>\n"
        f"Profit target: {pf.get('profit_target_pct', 0):.1f}%\n"
        f"Daily profit cap: {pf.get('daily_profit_cap_pct', 0):.1f}%\n\n"
        f"<b>Drawdown</b>\n"
        f"Overall DD: {pf.get('max_drawdown_overall_pct', 0):.1f}%\n"
        f"Daily DD: {pf.get('max_drawdown_daily_pct', 0):.1f}% enforced (firm {pf.get('max_drawdown_daily_pct', 0) + 1.0:.1f}%, −1pp buffer)\n"
        f"Type: {'Static' if pf.get('drawdown_is_static') else 'Dynamic'}\n\n"
        f"<b>Other Rules</b>\n"
        f"Raw spread: {'Yes' if pf.get('raw_spread_account') else 'No'}\n"
        f"Profit sharing: {pf.get('profit_sharing_pct', 0):.1f}%\n"
        f"Min profit days: {pf.get('min_profit_days', 0)}\n"
        f"Consistency: {pf.get('consistency_threshold_pct', 0):.1f}% enforced (firm {pf.get('consistency_threshold_pct', 0) + 1.0:.1f}%, −1pp buffer)\n\n"
        f"<b>Baselines</b>\n"
        f"Prop: ${prop_b:,.2f}\n"
        f"Personal: {_money(pers_b, pers_ccy) if pers_b > 0 else 'Not set'}",
        parse_mode="HTML",
    )


async def _cmd_equity(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    with _pf_lock:
        pf = dict(_propfirm)
    # Risk baseline = the prop-side anchor that drives lot sizing + ALL kill
    # levels (set via /setbaseline). The personal account has no kill conditions,
    # so there is no personal risk baseline. Initial deposit = actual capital in
    # the account (set via /setdeposit), used purely for equity % + fee math.
    risk_baseline = pf.get("baseline_equity", 0.0)
    prop_deposit  = pf.get("prop_initial_deposit", risk_baseline)
    pers_deposit  = pf.get("pers_initial_deposit", pf.get("pers_baseline_equity", 0.0))

    def _account_block(label: str, data: dict, deposit: float,
                       currency: str = "USD", risk: float | None = None) -> str:
        bal      = data["balance"]
        eq       = data["equity"]
        floating = data.get("profit", eq - bal)
        lines = [f"<b>{label}</b>"]
        # Risk baseline only printed for the prop side (it's the kill/sizing anchor).
        if risk is not None:
            lines.append(
                f"Risk baseline: {_money(risk, currency)}" if risk > 0
                else "Risk baseline: Not set — run /setbaseline"
            )
        # Initial deposit = configured actual capital. Shown so equity % is
        # anchored to real money in (set/correct it with /setdeposit).
        lines.append(
            f"Deposit: {_money(deposit, currency)}" if deposit > 0
            else "Deposit: Not set — run /setdeposit"
        )
        lines += [
            f"Balance: {_money(bal, currency)}",
            f"Equity: {_money(eq, currency)}",
            f"Floating: {_money(floating, currency, signed=True)}",
        ]
        # Trading Fee = every broker cost (commission + swap + any fee), derived
        # by reconciliation in the worker (balance − MT5 deposit − gross trade
        # P&L). Computed on-demand only (want_fee), never the 30 s poll. Absent
        # if the worker is still on old code → row simply hidden.
        if "trading_fee_total" in data:
            lines.append(f"Trading Fee: {_money(data['trading_fee_total'], currency, signed=True)}")
        # Overall performance is measured against the initial deposit (real money
        # in), not the risk baseline.
        if deposit > 0:
            overall = eq - deposit
            lines.append(
                f"Overall: {_money(overall, currency, signed=True)} "
                f"({overall / deposit * 100:+.2f}%)"
            )
        return "\n".join(lines)

    try:
        pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "", True)
        pers_block = _account_block("Personal Signal", pers, pers_deposit,
                                    pers.get("account_currency", "USD"))
    except Exception as exc:
        pers_block = f"<b>Personal Signal</b>\nOffline — {exc}"

    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "", True)
        prop_block = _account_block("Prop Hedge", prop, prop_deposit, "USD", risk=risk_baseline)
    except Exception as exc:
        prop_block = f"<b>Prop Hedge</b>\nOffline — {exc}"

    await update.message.reply_text(
        f"{_cmd_header('📊 <b>Account Equity Snapshot</b>')}{pers_block}\n\n{prop_block}",
        parse_mode="HTML",
    )


async def _cmd_setbaseline(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the PROP risk baseline (baseline_equity) — the kill/sizing anchor.

    baseline_equity is the single value that drives lot sizing (0.67% of it) and
    EVERY kill level (K1–K5). It is a prop-only concept — the personal account
    has no kill conditions and its lots are derived from the prop geometry, so
    there is no personal risk baseline. (Actual deposited capital, used only for
    equity % + fee reporting, is set separately via /setdeposit.)

    Immutable-by-design — only deliberate operator action writes it. Sanctioned
    writers: /changepropfirm, /phase2, and this quick-fix command.

    Usage:
        /setbaseline            → show current risk baseline + usage
        /setbaseline 5000       → set prop baseline_equity to $5,000
    """
    if not _auth(update):
        return
    args = (update.message.text or "").split()[1:]

    with _pf_lock:
        pf = dict(_propfirm)
    current = pf.get("baseline_equity", 0.0)

    if len(args) != 1:
        cur_str = _money(current, "USD") if current > 0 else "Not set"
        await update.message.reply_text(
            f"{_cmd_header('📊 <b>Set Risk Baseline</b>')}"
            f"Current prop risk baseline: {cur_str}\n\n"
            "Drives lot sizing (0.67%) and all kill levels (K1–K5). Prop only — "
            "the personal account has no kill conditions.\n\n"
            "<b>Usage</b>\n"
            "<code>/setbaseline 5000</code>\n\n"
            "<i>For actual deposited capital (equity % + fees), use /setdeposit.</i>",
            parse_mode="HTML",
        )
        return
    try:
        amount = float(args[0].replace(",", ""))
        assert amount > 0
    except Exception:
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Invalid Amount</b>')}"
            "Enter a positive number.\n"
            "Example: <code>/setbaseline 5000</code>",
            parse_mode="HTML",
        )
        return

    with _pf_lock:
        _propfirm["baseline_equity"] = round(amount, 2)
        _save_propfirm(_propfirm)

    old_str = _money(current, "USD") if current > 0 else "Not set"
    await update.message.reply_text(
        f"{_cmd_header('✅ <b>Risk Baseline Updated</b>')}"
        f"Before: {old_str}\n"
        f"After: <b>{_money(amount, 'USD')}</b>\n\n"
        "<i>Lot sizing and all kill levels (K1–K5) now recompute from this.</i>",
        parse_mode="HTML",
    )
    logger.warning("Telegram: prop baseline_equity set to %.2f via /setbaseline", amount)


async def _cmd_setdayroll(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the prop firm's daily-loss reset time (propfirm_day_roll, SGT).

    The firm resets the Maximum Daily Loss at a FIXED wall-clock time that varies
    by account (FundingPips shows it as "Resets In" on the dashboard). This is the
    boundary at which the bot re-snapshots day_start_equity and auto-resumes after
    a daily-loss kill (K1) — it is NOT a rolling 24h from the last trade. Set it to
    match your account exactly.

    Safety: erring LATE is safe (bot stays halted longer than needed); erring EARLY
    re-opens the daily allowance before the firm does and risks a daily-DD breach —
    if unsure, pad a few minutes AFTER the firm's displayed reset. Set at setup, not
    mid-drawdown (changing it re-anchors the current day's starting equity).

    Usage:
        /setdayroll            → show current reset time
        /setdayroll 05:00      → set daily reset to 05:00 SGT
    """
    if not _auth(update):
        return
    args = (update.message.text or "").split()[1:]

    with _pf_lock:
        current = _propfirm.get("propfirm_day_roll", "11:00")

    if len(args) != 1:
        await update.message.reply_text(
            f"{_cmd_header('🕔 <b>Daily Reset Time</b>')}"
            f"Current prop daily-loss reset: <b>{current} SGT</b>\n\n"
            "The fixed time the firm resets Maximum Daily Loss (FundingPips: the "
            "\"Resets In\" countdown). The bot re-snapshots day-start equity and "
            "auto-resumes K1 at this boundary.\n\n"
            "<b>Usage</b>\n"
            "<code>/setdayroll 05:00</code>\n\n"
            "<i>Match your account exactly. If unsure, set a few minutes AFTER the "
            "firm's displayed reset — erring early risks a daily-DD breach.</i>",
            parse_mode="HTML",
        )
        return

    raw = args[0]
    try:
        h, m = map(int, raw.split(":"))
        assert 0 <= h <= 23 and 0 <= m <= 59
        new_val = f"{h:02d}:{m:02d}"
    except Exception:
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Invalid Time</b>')}"
            "Enter a 24-hour SGT time as HH:MM.\n"
            "Example: <code>/setdayroll 05:00</code>",
            parse_mode="HTML",
        )
        return

    with _pf_lock:
        _propfirm["propfirm_day_roll"] = new_val
        _save_propfirm(_propfirm)

    await update.message.reply_text(
        f"{_cmd_header('✅ <b>Daily Reset Time Updated</b>')}"
        f"Before: {current} SGT\n"
        f"After: <b>{new_val} SGT</b>\n\n"
        "<i>Day-start equity re-snapshot and K1 auto-resume now anchor to this "
        "time. Verify it matches your prop dashboard's reset exactly.</i>",
        parse_mode="HTML",
    )
    logger.warning("Telegram: propfirm_day_roll set to %s via /setdayroll", new_val)


async def _cmd_setdeposit(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the initial deposit (actual capital) for an account.

    Separate from the risk baseline (/setbaseline) on purpose: this is the real
    money in the account, used ONLY for equity % and the trading-fee math — it
    never touches lot sizing or kill levels. Keeping the two apart lets a manual
    run-up before the bot took over be reflected in reporting without distorting
    risk: e.g. deposit 486.88 personal even though the risk anchor is the prop's
    5,000.

    Usage:
        /setdeposit                   → show current deposits + actual MT5
                                        deposit (for reference) + usage
        /setdeposit prop 5000         → set prop initial deposit (USD)
        /setdeposit personal 486.88   → set personal initial deposit (account ccy)
    """
    if not _auth(update):
        return
    args = (update.message.text or "").split()[1:]

    with _pf_lock:
        pf = dict(_propfirm)
    prop_dep = pf.get("prop_initial_deposit", pf.get("baseline_equity", 0.0))
    pers_dep = pf.get("pers_initial_deposit", pf.get("pers_baseline_equity", 0.0))

    if len(args) != 2:
        # Pull the actual MT5 deposit (want_fee=True returns deposit_total) as a
        # hint for what to set.
        try:
            pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "", True)
            pers_ccy = pers.get("account_currency", "USD")
            pers_mt5 = pers.get("deposit_total")
        except Exception:
            pers_ccy, pers_mt5 = "USD", None
        try:
            prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "", True)
            prop_mt5 = prop.get("deposit_total")
        except Exception:
            prop_mt5 = None
        prop_line = f"Prop deposit: {_money(prop_dep, 'USD') if prop_dep > 0 else 'Not set'}"
        if prop_mt5:
            prop_line += f"\nProp MT5 deposit (actual): {_money(prop_mt5, 'USD')}"
        pers_line = f"Personal deposit: {_money(pers_dep, pers_ccy) if pers_dep > 0 else 'Not set'}"
        if pers_mt5:
            pers_line += f"\nPersonal MT5 deposit (actual): {_money(pers_mt5, pers_ccy)}"
        await update.message.reply_text(
            f"{_cmd_header('📊 <b>Set Initial Deposit</b>')}"
            f"{prop_line}\n\n{pers_line}\n\n"
            "<b>Usage</b>\n"
            "<code>/setdeposit prop 5000</code>\n"
            "<code>/setdeposit personal 486.88</code>\n\n"
            "<i>Used for equity % + trading-fee reporting only. Does NOT affect "
            "lot sizing or kill levels (that's /setbaseline).</i>",
            parse_mode="HTML",
        )
        return

    side = args[0].lower()
    if side not in ("prop", "personal"):
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Unknown Account</b>')}"
            "First argument must be <code>prop</code> or <code>personal</code>.\n"
            "Example: <code>/setdeposit personal 486.88</code>",
            parse_mode="HTML",
        )
        return
    try:
        amount = float(args[1].replace(",", ""))
        assert amount > 0
    except Exception:
        await update.message.reply_text(
            f"{_cmd_header('⚠️ <b>Invalid Amount</b>')}"
            "Enter a positive number.\n"
            "Example: <code>/setdeposit personal 486.88</code>",
            parse_mode="HTML",
        )
        return

    key      = "prop_initial_deposit" if side == "prop" else "pers_initial_deposit"
    label    = "Prop Hedge" if side == "prop" else "Personal Signal"
    currency = "USD"
    if side == "personal":
        try:
            pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
            currency = pers.get("account_currency", "USD")
        except Exception:
            currency = "USD"
    old = prop_dep if side == "prop" else pers_dep

    with _pf_lock:
        _propfirm[key] = round(amount, 2)
        _save_propfirm(_propfirm)

    old_str = _money(old, currency) if old > 0 else "Not set"
    await update.message.reply_text(
        f"{_cmd_header('✅ <b>Initial Deposit Updated</b>')}"
        f"Account: {label}\n"
        f"Before: {old_str}\n"
        f"After: <b>{_money(amount, currency)}</b>\n\n"
        "<i>Used for equity % + trading-fee reporting. Risk math is unchanged.</i>",
        parse_mode="HTML",
    )
    logger.warning("Telegram: %s %s set to %.2f via /setdeposit", label, key, amount)


async def _cmd_emergency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END

    try:
        prop_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
        prop_err = None
    except Exception as exc:
        prop_pos = []
        prop_err = str(exc)
    try:
        pers_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS)
        pers_err = None
    except Exception as exc:
        pers_pos = []
        pers_err = str(exc)

    pers_ccy = await _pers_currency()
    body = (
        "This will:\n"
        "• Force-close all open positions\n"
        "• Halt signal processing\n\n"
        "<b>Open Positions</b>\n\n"
        f"{_cmd_pos_block('Personal Signal', pers_pos, pers_err, pers_ccy)}\n\n"
        f"{_cmd_pos_block('Prop Hedge', prop_pos, prop_err, 'USD')}\n\n"
        "Reply <code>CONFIRM</code> to proceed.\n"
        "Send /cancel to abort."
    )
    await update.message.reply_text(
        f"{_cmd_header('🔴 <b>Emergency Halt — Confirm Action</b>')}{body}",
        parse_mode="HTML",
    )
    return EMERGENCY_CONFIRM


async def _emergency_execute(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    if (update.message.text or "").strip() != "CONFIRM":
        await update.message.reply_text(
            "⚠️ <b>Confirmation Required</b>\n\nType <code>CONFIRM</code> to proceed, or /cancel to abort.",
            parse_mode="HTML",
        )
        return EMERGENCY_CONFIRM
    await asyncio.to_thread(_dispatch_force_close, "emergency_halt", halt=True)
    await asyncio.sleep(2)
    try:
        prop_eq  = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        pers_eq  = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        eq_lines = (
            f"Personal: {_money(pers_eq['equity'], pers_eq.get('account_currency', 'USD'))}\n"
            f"Prop: ${prop_eq['equity']:,.2f}"
        )
    except Exception:
        eq_lines = "Could not query equity"
    await update.message.reply_text(
        f"{_cmd_header('🔴 <b>Emergency Halt Executed</b>')}"
        f"<b>Action Taken</b>\n"
        f"All positions force-closed.\n"
        f"Signal processing halted.\n\n"
        f"<b>Equity After Close</b>\n{eq_lines}\n\n"
        f"<b>Next Step</b>\nUse /resume only after confirming both accounts are safe.",
        parse_mode="HTML",
    )
    logger.warning("Telegram: emergency halt executed by user")
    return ConversationHandler.END


async def _emergency_abort(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    await update.message.reply_text(
        f"{_cmd_header('🟡 <b>Emergency Cancelled</b>')}No positions were closed.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_positions(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        prop_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
        prop_err = None
    except Exception as exc:
        prop_pos = None
        prop_err = str(exc)
    try:
        pers_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS)
        pers_err = None
    except Exception as exc:
        pers_pos = None
        pers_err = str(exc)

    pers_ccy   = await _pers_currency()
    pers_block = _cmd_pos_block("Personal Signal", pers_pos, pers_err, pers_ccy)
    prop_block = _cmd_pos_block("Prop Hedge", prop_pos, prop_err, "USD")

    await update.message.reply_text(
        f"{_cmd_header('📊 <b>Open Positions</b>')}"
        f"{pers_block}\n\n{prop_block}",
        parse_mode="HTML",
    )


async def _cmd_pnl(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
    except Exception as exc:
        await update.message.reply_text(f"⚠️ <b>Prop Worker Offline</b>\n\n<code>{exc}</code>", parse_mode="HTML")
        return
    with _pf_lock:
        pf = dict(_propfirm)

    baseline    = pf.get("baseline_equity",         0.0)
    day_start   = pf.get("day_start_equity",         0.0)
    daily_cap   = pf.get("daily_profit_cap_pct",     0.0)
    daily_dd    = pf.get("max_drawdown_daily_pct",   0.0)
    overall_dd  = pf.get("max_drawdown_overall_pct", 0.0)
    target_pct  = pf.get("profit_target_pct",        0.0)
    equity      = prop["equity"]

    overall_pnl    = equity - baseline
    daily_pnl      = equity - day_start
    # K1 daily loss is DYNAMIC: % of day_start, not baseline. Resets each session.
    daily_loss_amt = round(day_start  * daily_dd  / 100.0, 2) if daily_dd  > 0 and day_start > 0 else 0.0
    daily_cap_amt  = round(baseline   * daily_cap / 100.0, 2) if daily_cap > 0 else 0.0
    overall_dd_amt = round(baseline   * overall_dd / 100.0, 2) if overall_dd > 0 else 0.0
    target_amt     = round(baseline   * target_pct / 100.0, 2) if target_pct > 0 else 0.0

    overall_floor   = baseline - overall_dd_amt
    daily_floor     = day_start - daily_loss_amt if daily_loss_amt > 0 else 0.0
    daily_cap_level = day_start + daily_cap_amt
    k4_target       = baseline + target_amt
    daily_remaining = max(0.0, daily_cap_level - equity)

    # K1 bar: % of today's daily loss limit consumed (from day_start down to daily_floor)
    k1_consumed = max(0.0, day_start - equity)
    k1_bar_pct  = k1_consumed / daily_loss_amt * 100 if daily_loss_amt > 0 else 0.0
    k1_status   = "🔴 BREACHED" if equity <= daily_floor else "🟢 Active"

    # K2 bar: % of overall DD consumed from baseline
    k2_consumed = max(0.0, baseline - equity)
    k2_bar_pct  = k2_consumed / overall_dd_amt * 100 if overall_dd_amt > 0 else 0.0

    # K3 bar: % of daily cap consumed today
    k3_consumed = max(0.0, equity - day_start)
    k3_bar_pct  = k3_consumed / daily_cap_amt * 100 if daily_cap_amt > 0 else 0.0

    # K4 bar: % progress toward overall profit target
    k4_bar_pct  = max(0.0, overall_pnl) / target_amt * 100 if target_amt > 0 else 0.0

    lines = [
        f"<b>Account</b>",
        f"Baseline: ${baseline:,.2f}",
        f"Day-start: ${day_start:,.2f}",
        f"Current equity: ${equity:,.2f}",
    ]
    if daily_loss_amt > 0:
        lines += [
            f"\n<b>K1/K2 — Loss Protection</b>",
            f"Daily limit: ${daily_loss_amt:,.2f} ({daily_dd:.1f}% of day-start)  |  {k1_status}",
            f"Daily floor: <b>${daily_floor:,.2f}</b>  (resets each session)",
            f"Overall DD floor: ${overall_floor:,.2f}  (static from baseline)",
            f"<code>{_pnl_bar(k1_bar_pct)}</code> of daily limit used",
            f"<code>{_pnl_bar(k2_bar_pct)}</code> of overall DD consumed",
        ]
    if daily_cap_amt > 0:
        k3_status = "🔴 CAP HIT" if equity >= daily_cap_level else "🟢 Active"
        lines += [
            f"\n<b>K3 — Profit Control</b>",
            f"Daily cap: {daily_cap:.1f}% of baseline  =  ${daily_cap_amt:,.2f}",
            f"Day-start equity: ${day_start:,.2f}",
            f"Daily cap level: <b>${daily_cap_level:,.2f}</b>  |  {k3_status}",
            f"Current equity: ${equity:,.2f}",
            f"Remaining today: <b>${daily_remaining:,.2f}</b>",
            f"<code>{_pnl_bar(k3_bar_pct)}</code> of daily cap used",
        ]
    if target_amt > 0:
        lines += [
            f"\n<b>K4 — Overall Target</b>",
            f"Target: {target_pct:.1f}% of baseline  =  ${target_amt:,.2f}",
            f"Target level: <b>${k4_target:,.2f}</b>",
            f"Progress: ${max(0.0, overall_pnl):,.2f} / ${target_amt:,.2f}",
            f"<code>{_pnl_bar(k4_bar_pct)}</code>",
        ]

    lines.append(
        "\n<i>Bars — K1: daily loss used (% of day-start, resets each session) · "
        "K2: total DD consumed from baseline · "
        "K3: daily profit cap used · "
        "K4: progress toward profit target</i>"
    )

    await update.message.reply_text(
        _cmd_header("📊 <b>P&amp;L Dashboard — Prop Hedge</b>") + "\n".join(lines),
        parse_mode="HTML",
    )


async def _cmd_health(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://127.0.0.1:8000/health")
        l1 = "🟢 Alive" if resp.status_code == 200 else f"⚠️ HTTP {resp.status_code}"
    except Exception as exc:
        l1 = f"🔴 Offline — {exc}"
    try:
        await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_h = "🟢 Alive"
    except Exception as exc:
        prop_h = f"🔴 Offline — {exc}"
    try:
        await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_h = "🟢 Alive"
    except Exception as exc:
        pers_h = f"🔴 Offline — {exc}"

    await update.message.reply_text(
        f"{_cmd_header('📊 <b>System Health</b>')}"
        f"Layer 1 (VPS #1): {l1}\n"
        f"Layer 2 (VPS #1): 🟢 Alive\n"
        f"Personal Signal (VPS #2): {pers_h}\n"
        f"Prop Hedge (VPS #3): {prop_h}",
        parse_mode="HTML",
    )


def _fmt_checksymbols_leg(label: str, rep: dict) -> str:
    """One account's symbol-resolution block for /checksymbols."""
    if rep.get("error"):
        return f"<b>{label}</b>: 🔴 {rep['error']}\n"
    supported = rep.get("supported", [])
    found     = rep.get("found", [])
    missing   = rep.get("missing", [])
    mapping   = rep.get("mapping", {})
    n_sup, n_found = len(supported), len(found)
    out = (
        f"<b>{label}</b>\n"
        f"Supported: {n_sup}   Found: {n_found}   Missing: {n_sup - n_found}\n"
    )
    # Reveal the broker's discovered suffix on a few non-identity mappings.
    examples = [f"{c}→{b}" for c, b in mapping.items() if b != c][:4]
    if examples:
        out += f"Broker naming: <code>{', '.join(examples)}</code>\n"
    if missing:
        out += f"❌ Missing: <code>{', '.join(missing)}</code>\n"
    return out


async def _cmd_checksymbols(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Per-broker symbol-resolution health check. Asks each Layer 3 worker which
    canonical tickers resolved to a real MT5 symbol and which are MISSING."""
    if not _auth(update):
        return
    pers = await asyncio.to_thread(_query_checksymbols, ZMQ_REQ_PERS)
    prop = await asyncio.to_thread(_query_checksymbols, ZMQ_REQ_PROP)
    await update.message.reply_text(
        f"{_cmd_header('🧭 <b>Symbol Check</b>')}"
        f"{_fmt_checksymbols_leg('Personal (VPS #2)', pers)}\n"
        f"{_fmt_checksymbols_leg('Prop (VPS #3)', prop)}\n"
        f"<i>Only arm a TradingView alert for pairs shown FOUND on the broker "
        f"that trades them.</i>",
        parse_mode="HTML",
    )


async def _cmd_news(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        events = await asyncio.to_thread(_fetch_ff_events)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ <b>News Calendar Error</b>\n\n<code>{exc}</code>", parse_mode="HTML")
        return

    now     = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=4)
    sgt_off = timedelta(hours=8)

    ccy_to_pairs: dict[str, list[str]] = {}
    for ticker, ccys in _TICKER_CURRENCIES.items():
        for c in ccys:
            ccy_to_pairs.setdefault(c, []).append(ticker)

    relevant = []
    for ev in events:
        if ev.get("impact") != "High":
            continue
        t = ev.get("time_utc")
        if t is None or not (now <= t <= horizon):
            continue
        ccy   = ev.get("currency", "")
        pairs = ccy_to_pairs.get(ccy, [])
        relevant.append((t, ccy, ev.get("title", ""), pairs))
    relevant.sort(key=lambda x: x[0])

    if not relevant:
        await update.message.reply_text(
            f"{_cmd_header('📰 <b>Upcoming High-Impact News</b>')}"
            "🟢 No high-impact events in the next 4 hours for covered pairs.",
            parse_mode="HTML",
        )
        return

    lines = []
    for t, ccy, title, pairs in relevant:
        sgt_str   = (t + sgt_off).strftime("%H:%M SGT")
        pairs_str = ", ".join(pairs) if pairs else "—"
        lines.append(f"🟠 {sgt_str} — {ccy}: {title}\nAffects: {pairs_str}")

    await update.message.reply_text(
        _cmd_header("📰 <b>Upcoming High-Impact News</b>")
        + "<i>Next 4 hours · Covered pairs only</i>\n\n"
        + "\n\n".join(lines),
        parse_mode="HTML",
    )


async def _cmd_blackboard(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    now     = datetime.now(timezone.utc)
    sgt_off = timedelta(hours=8)

    with _news_suppressed_lock:
        news_active = {t: e for t, e in _news_suppressed_pairs.items() if e > now}
    with _manual_suppress_lock:
        manual_active = set(_manual_suppressed_pairs)

    all_pairs = set(news_active) | manual_active
    if not all_pairs:
        await update.message.reply_text(
            f"{_cmd_header('📊 <b>Suppression Blackboard</b>')}"
            "🟢 No active suppressions.\n"
            "All covered pairs are clear for new signals.",
            parse_mode="HTML",
        )
        return

    blocks: list[str] = []
    for ticker in sorted(all_pairs):
        entry_lines = [f"🔴 <b>{ticker}</b>"]
        if ticker in news_active:
            ends_sgt = (news_active[ticker] + sgt_off).strftime("%H:%M SGT")
            entry_lines.append(f"Source: News suppression")
            entry_lines.append(f"Blocked until: {ends_sgt} SGT")
        if ticker in manual_active:
            entry_lines.append(f"Source: Manual block")
            entry_lines.append(f"Unblock: /resumepair {ticker}")
        blocks.append("\n".join(entry_lines))

    await update.message.reply_text(
        _cmd_header("📊 <b>Suppression Blackboard</b>") + "\n\n".join(blocks),
        parse_mode="HTML",
    )


async def _cmd_closepair(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        await update.message.reply_text("Usage: <code>/closepair EURUSD</code>", parse_mode="HTML")
        return ConversationHandler.END
    ticker = text[1].upper()
    if ticker not in ALLOWED_PAIRS:
        await update.message.reply_text(
            f"⚠️ <b>Unknown Pair</b>\n\n"
            f"Pair: <code>{ticker}</code>\n"
            f"Allowed: {', '.join(sorted(ALLOWED_PAIRS))}",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    ctx.chat_data["closepair_ticker"] = ticker
    broker_symbol = _SYMBOL_MAP.get(ticker, ticker)

    syms     = (ticker, broker_symbol)
    pers_ccy = await _pers_currency()
    try:
        pers_pos   = [p for p in await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS) if p["symbol"] in syms]
        pers_block = _cmd_pos_block("Personal Signal", pers_pos, None, pers_ccy)
    except Exception as exc:
        pers_block = f"<b>Personal Signal</b>\nOffline — {exc}"
    try:
        prop_pos   = [p for p in await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP) if p["symbol"] in syms]
        prop_block = _cmd_pos_block("Prop Hedge", prop_pos, None, "USD")
    except Exception as exc:
        prop_block = f"<b>Prop Hedge</b>\nOffline — {exc}"

    body = (
        "This will:\n"
        "• Close all positions\n"
        "• Block new signals\n\n"
        "<b>Open Positions</b>\n\n"
        f"{pers_block}\n\n{prop_block}\n\n"
        "Reply <code>CONFIRM</code> to proceed.\n"
        "Send /cancel to abort."
    )
    await update.message.reply_text(
        f"{_cmd_header(f'🟡 <b>Close Pair — {ticker}</b>')}{body}",
        parse_mode="HTML",
    )
    return CLOSEPAIR_CONFIRM


async def _closepair_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    if (update.message.text or "").strip() != "CONFIRM":
        await update.message.reply_text(
            "⚠️ <b>Confirmation Required</b>\n\nType <code>CONFIRM</code> to proceed, or /cancel to abort.",
            parse_mode="HTML",
        )
        return CLOSEPAIR_CONFIRM
    ticker = ctx.chat_data.get("closepair_ticker", "")
    await asyncio.to_thread(_dispatch_close_ticker, ticker, "manual_closepair")
    with _manual_suppress_lock:
        _manual_suppressed_pairs.add(ticker)
    _dispatch_news_suppress(ticker, datetime(9999, 12, 31, tzinfo=timezone.utc))
    await asyncio.sleep(2)
    try:
        prop_eq  = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        pers_eq  = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        eq_lines = (
            f"Personal: {_money(pers_eq['equity'], pers_eq.get('account_currency', 'USD'))}\n"
            f"Prop: ${prop_eq['equity']:,.2f}"
        )
    except Exception:
        eq_lines = "Could not query equity"
    await update.message.reply_text(
        f"{_cmd_header(f'✅ <b>Pair Closed and Blocked — {ticker}</b>')}"
        f"<b>Action Taken</b>\n"
        f"All {ticker} positions closed.\n"
        f"New {ticker} signals are blocked.\n\n"
        f"<b>Equity After Close</b>\n{eq_lines}\n\n"
        f"<b>To Unblock</b>\n/resumepair {ticker}",
        parse_mode="HTML",
    )
    logger.warning("Manual closepair executed: %s", ticker)
    return ConversationHandler.END


async def _closepair_abort(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    ticker = ctx.chat_data.get("closepair_ticker", "")
    await update.message.reply_text(
        f"{_cmd_header(f'🟡 <b>Close Pair Cancelled — {ticker}</b>')}No positions were closed.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_resumepair(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        await update.message.reply_text("Usage: <code>/resumepair EURUSD</code>", parse_mode="HTML")
        return
    ticker = text[1].upper()
    if ticker not in ALLOWED_PAIRS:
        await update.message.reply_text(
            f"⚠️ <b>Unknown Pair</b>\n\n"
            f"Pair: <code>{ticker}</code>\n"
            f"Allowed: {', '.join(sorted(ALLOWED_PAIRS))}",
            parse_mode="HTML",
        )
        return

    with _manual_suppress_lock:
        _manual_suppressed_pairs.discard(ticker)
    _dispatch_news_clear(ticker)

    await update.message.reply_text(
        f"{_cmd_header(f'🟢 <b>Pair Resumed — {ticker}</b>')}New {ticker} signals are now allowed.",
        parse_mode="HTML",
    )
    logger.info("Manual resumepair: %s", ticker)


async def _cmd_setmaxpos(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        await update.message.reply_text(
            "⚠️ <b>Invalid Position Limit</b>\n\n"
            "Enter a whole number from 1 to 10.\n"
            "Example: /setmaxpos 2",
            parse_mode="HTML",
        )
        return
    try:
        n = int(text[1])
        assert 1 <= n <= 10
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Position Limit</b>\n\n"
            "Enter a whole number from 1 to 10.\n"
            "Example: /setmaxpos 2",
            parse_mode="HTML",
        )
        return

    with _state_lock:
        old_max = _phase_state.get("max_open_positions", 2)
        _phase_state["max_open_positions"] = n
        _save_phase(_phase_state)

    await update.message.reply_text(
        f"{_cmd_header('📊 <b>Max Position Limit Updated</b>')}"
        f"Before: {old_max}\n"
        f"After: <b>{n}</b>",
        parse_mode="HTML",
    )
    if n > 5:
        theoretical = round(n * PROP_RISK_PCT * 100, 2)
        await update.message.reply_text(
            f"⚠️ <b>Risk Warning</b>\n"
            f"{n} positions may create high combined exposure if all SLs hit together.",
            parse_mode="HTML",
        )
    logger.info("Telegram: max_open_positions set to %d", n)


async def _cmd_maxpos(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        limit = _phase_state.get("max_open_positions", 2)
    try:
        prop_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
        count_str = str(len(prop_pos))
    except Exception as exc:
        count_str = f"unknown ({exc})"
    await update.message.reply_text(
        f"{_cmd_header('📊 <b>Position Limit</b>')}"
        f"Max allowed: {limit}\n"
        f"Currently open: {count_str}",
        parse_mode="HTML",
    )


async def _cmd_consistency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        phase = int(_phase_state.get("phase", 1))

    if phase != 2:
        await update.message.reply_text(
            f"{_cmd_header('📊 <b>Consistency Tracker</b>')}"
            "Not active.\n"
            "Consistency tracking is Phase 2 only.\n\n"
            "<b>Next step</b>\n/phase2",
            parse_mode="HTML",
        )
        return

    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_equity = prop["equity"]
    except Exception as exc:
        await update.message.reply_text(f"⚠️ <b>Prop Worker Offline</b>\n\n<code>{exc}</code>", parse_mode="HTML")
        return

    with _pf_lock:
        pf = dict(_propfirm)

    day_start = pf.get("day_start_equity",          0.0)
    baseline  = pf.get("baseline_equity",            0.0)
    threshold = pf.get("consistency_threshold_pct",  0.0)

    with _cons_lock:
        locked_days = list(_consistency_log.get("days", []))

    today_date    = _propfirm_day(_sgt_now())
    today_running = prop_equity - day_start if day_start > 0 else 0.0

    table_str, total, max_day_val, ratio_pct, rule_met = _build_consistency_table(
        locked_days, today_running, today_date, baseline, threshold,
    )

    if rule_met:
        await update.message.reply_text(
            f"{_cmd_header('🟢 <b>Consistency Rule Met</b>')}"
            f"{table_str}\n\n"
            f"Ready to submit payout claim.",
            parse_mode="HTML",
        )
        return

    days_with_profit = len(locked_days) + (1 if today_running > 0 else 0)
    if days_with_profit < 2:
        status_line = "Need at least 2 profitable days to evaluate."
    else:
        status_line = f"Not met yet — largest day is {ratio_pct:.1f}% of total profit (need &lt; {threshold:.1f}%)."

    await update.message.reply_text(
        f"{_cmd_header('📊 <b>Consistency Tracker</b>')}"
        f"Threshold: &lt; {threshold:.1f}%\n\n"
        f"{table_str}\n\n"
        f"<b>Status</b>\n{status_line}",
        parse_mode="HTML",
    )


async def _cmd_setwindow(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    args = (update.message.text or "").split()[1:]
    if len(args) != 2:
        await update.message.reply_text(
            f"{_cmd_header('🕒 <b>Trading Window Usage</b>')}"
            "Format: <code>/setwindow HH:MM HH:MM</code>\n"
            "Example: <code>/setwindow 09:00 00:00</code>\n"
            "Note: <code>00:00</code> = midnight",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    start_str, end_str = args[0], args[1]
    try:
        for t in (start_str, end_str):
            h, m = map(int, t.split(":"))
            assert 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Time Format</b>\n\n"
            "Use HH:MM in 24-hour SGT time.\n"
            "Example: <code>/setwindow 09:00 00:00</code>",
            parse_mode="HTML",
        )
        return ConversationHandler.END
    with _window_lock:
        curr = dict(_trading_window["current_window"])
    _setwindow_data.clear()
    _setwindow_data["start"] = start_str
    _setwindow_data["end"]   = end_str
    await update.message.reply_text(
        f"{_cmd_header('🕒 <b>Update Trading Window</b>')}"
        f"<b>New Window</b>\n{start_str}–{end_str} SGT\n\n"
        f"<b>Current Window</b>\n{curr['start']}–{curr['end']} SGT\n\n"
        "<b>Apply When?</b>\n"
        "<b>1</b> — Today, effective immediately\n"
        "<b>2</b> — Tomorrow, next session rollover at 11:00 SGT",
        parse_mode="HTML",
    )
    return SETWINDOW_CONFIRM


async def _setwindow_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    choice = update.message.text.strip().lower()
    start  = _setwindow_data.get("start", "12:00")
    end    = _setwindow_data.get("end",   "00:00")
    new_window = {"start": start, "end": end}
    if choice in ("1", "today"):
        with _window_lock:
            _trading_window["current_window"] = new_window
            _trading_window["next_window"]    = None
            _save_trading_window()
        await update.message.reply_text(
            f"{_cmd_header('🕒 <b>Trading Window Updated</b>')}"
            f"Applied: Today, effective immediately\n"
            f"Window: <b>{start}–{end} SGT</b>",
            parse_mode="HTML",
        )
    elif choice in ("2", "tomorrow"):
        with _window_lock:
            _trading_window["next_window"] = new_window
            _save_trading_window()
        await update.message.reply_text(
            f"{_cmd_header('🕒 <b>Trading Window Scheduled</b>')}"
            f"Applied: Tomorrow, next session rollover at 11:00 SGT\n"
            f"Window: <b>{start}–{end} SGT</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "⚠️ <b>Invalid Selection</b>\n\nReply <b>1</b> for today or <b>2</b> for tomorrow. Or /cancel to abort.",
            parse_mode="HTML",
        )
        return SETWINDOW_CONFIRM
    _setwindow_data.clear()
    return ConversationHandler.END


async def _setwindow_abort(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _setwindow_data.clear()
    await update.message.reply_text(
        f"{_cmd_header('🟡 <b>Cancelled</b>')}No changes were applied.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_cancel_noop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    await update.message.reply_text(
        f"{_cmd_header('🟡 <b>Nothing to Cancel</b>')}No active wizard is running.",
        parse_mode="HTML",
    )


# ── /changeaccount wizard ─────────────────────────────────────────────────

def _changeaccount_text_personal() -> str:
    return (
        f"{_cmd_header('🔧 <b>Change Personal Signal MT5 Account</b>')}"
        "1. Open PowerShell on the Personal worker VPS\n\n"
        "2. Stop the current worker with Ctrl+C\n"
        "   If that does not work, close the PowerShell window\n\n"
        "3. Open the env file:\n"
        "<code>notepad C:\\arbitrage\\.env</code>\n\n"
        "4. Update:\n"
        "<code>MT5_LOGIN=your_login</code>\n"
        "<code>MT5_PASSWORD=your_password</code>\n"
        "<code>MT5_SERVER=your_server</code>\n\n"
        "5. Save the file\n\n"
        "6. Restart the worker:\n"
        "<code>cd C:\\arbitrage</code>\n"
        "<code>uv run python layer3/worker_personal.py</code>\n\n"
        "7. Confirm logs show:\n"
        "<code>layer3.personal — MT5 connected — account=...</code>\n"
        "<code>layer3.personal — REP socket bound on tcp://0.0.0.0:5556</code>\n"
        "<code>layer3.personal — PULL socket bound on tcp://0.0.0.0:5555</code>"
    )


def _changeaccount_text_prop() -> str:
    return (
        f"{_cmd_header('🔧 <b>Change Prop Hedge MT5 Account</b>')}"
        "1. Open PowerShell on the Prop worker VPS\n\n"
        "2. Stop the current worker with Ctrl+C\n"
        "   If that does not work, close the PowerShell window\n\n"
        "3. Open the env file:\n"
        "<code>notepad C:\\arbitrage\\.env</code>\n\n"
        "4. Update:\n"
        "<code>MT5_LOGIN=your_login</code>\n"
        "<code>MT5_PASSWORD=your_password</code>\n"
        "<code>MT5_SERVER=your_server</code>\n\n"
        "5. Save the file\n\n"
        "6. Restart the worker:\n"
        "<code>cd C:\\arbitrage</code>\n"
        "<code>uv run python layer3/worker_prop.py</code>\n\n"
        "7. Confirm logs show:\n"
        "<code>layer3.prop — MT5 connected — account=...</code>\n"
        "<code>layer3.prop — REP socket bound on tcp://0.0.0.0:5556</code>\n"
        "<code>layer3.prop — PULL socket bound on tcp://0.0.0.0:5555</code>"
    )


# ── /checkaccount ─────────────────────────────────────────────────────────

async def _cmd_checkaccount(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    def _fmt_block(label: str, data: dict) -> str:
        login  = data.get("account_login")
        server = data.get("account_server")
        return (
            f"<b>{label}</b>\n"
            f"Status: connected\n"
            f"Login: {login if login is not None else '—'}\n"
            f"Server: {server if server else '—'}"
        )

    try:
        pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_block = _fmt_block("Personal Signal", pers)
    except Exception:
        pers_block = (
            "<b>Personal Signal</b>\n"
            "Status: offline\n"
            "Action: restart the worker manually on the Personal VPS"
        )

    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_block = _fmt_block("Prop Hedge", prop)
    except Exception:
        prop_block = (
            "<b>Prop Hedge</b>\n"
            "Status: offline\n"
            "Action: restart the worker manually on the Prop VPS"
        )

    await update.message.reply_text(
        f"{_cmd_header('📋 <b>Account Check</b>')}{pers_block}\n\n{prop_block}",
        parse_mode="HTML",
    )


# ── /update ───────────────────────────────────────────────────────────────

def _update_menu_text() -> str:
    return (
        f"{_cmd_header('🛠️ <b>Update Guide</b>')}"
        "Choose what you want to update:\n\n"
        "/update local — Push local code to GitHub\n"
        "/update layer2 — Deploy latest code to VPS #1\n"
        "/update layer3 — Update a Layer 3 worker\n"
        "/update account — MT5 account change checklist"
    )


def _update_local_text() -> str:
    return (
        f"{_cmd_header('🛠️ <b>Update Local Code → GitHub</b>')}"
        "Run on Mac terminal:\n\n"
        "<code>cd ~/arbitrage-trading</code>\n"
        "Go into the local project folder.\n\n"
        "<code>git status</code>\n"
        "Check which files changed before adding anything.\n\n"
        "<code>git add layer2/telegram_handlers.py</code>\n"
        "Stage the specific file you changed. Replace the path if you changed a different file.\n\n"
        "<code>git commit -m \"Describe your change\"</code>\n"
        "Save the staged changes as a Git commit.\n\n"
        "<code>git push origin main</code>\n"
        "Push the committed change to GitHub.\n\n"
        "<code>git status</code>\n"
        "Confirm your local repo is clean and up to date.\n\n"
        "<b>Good result</b>\n"
        "You want to see:\n"
        "<code>Your branch is up to date with 'origin/main'</code>\n\n"
        "<b>Note</b>\n"
        "Always run <code>git status</code> first so you only add files you actually changed."
    )


def _update_layer2_text() -> str:
    return (
        f"{_cmd_header('🛠️ <b>Deploy Layer 2 — VPS #1</b>')}"
        "Run on Mac terminal:\n\n"
        "<code>ssh root@152.42.213.98</code>\n"
        "Log in to the Linux VPS that runs Layer 1 / Layer 2.\n\n"
        "Then on VPS #1:\n\n"
        "<code>cd /root/arbitrage-trading</code>\n"
        "Go into the deployed project folder.\n\n"
        "<code>git pull</code>\n"
        "Pull the latest code from GitHub.\n\n"
        "<code>sudo systemctl restart layer2</code>\n"
        "Restart the Layer 2 Telegram/risk service.\n\n"
        "<code>systemctl status layer2</code>\n"
        "Check whether Layer 2 restarted successfully.\n\n"
        "<code>journalctl -u layer2 -f</code>\n"
        "Watch live Layer 2 logs for startup errors or normal activity.\n\n"
        "<b>Good result</b>\n"
        "Look for normal Telegram bot startup and no Python errors."
    )


def _update_personal_text() -> str:
    return (
        f"{_cmd_header('🛠️ <b>Update Personal Worker</b>')}"
        "On Personal worker VPS:\n\n"
        "<code>Ctrl+C</code>\n"
        "Stop the currently running worker in PowerShell. If this does not work, close the PowerShell window.\n\n"
        "<code>cd C:\\arbitrage</code>\n"
        "Go into the Windows worker project folder.\n\n"
        "<code>git pull</code>\n"
        "Pull the latest code from GitHub.\n\n"
        "<code>uv run python layer3/worker_personal.py</code>\n"
        "Restart the Personal Signal MT5 worker.\n\n"
        "<b>Good result</b>\n"
        "You should see:\n\n"
        "<code>layer3.personal — MT5 connected — account=...</code>\n"
        "Confirms the worker connected to MT5.\n\n"
        "<code>layer3.personal — REP socket bound on tcp://0.0.0.0:5556</code>\n"
        "Confirms Layer 2 can request data from this worker.\n\n"
        "<code>layer3.personal — PULL socket bound on tcp://0.0.0.0:5555</code>\n"
        "Confirms this worker can receive trade tickets.\n\n"
        "<b>Important</b>\n"
        "Closing the noVNC browser tab does not stop the VPS.\n"
        "Do not close PowerShell after the worker is running."
    )


def _update_prop_text() -> str:
    return (
        f"{_cmd_header('🛠️ <b>Update Prop Worker</b>')}"
        "On Prop worker VPS:\n\n"
        "<code>Ctrl+C</code>\n"
        "Stop the currently running worker in PowerShell. If this does not work, close the PowerShell window.\n\n"
        "<code>cd C:\\arbitrage</code>\n"
        "Go into the Windows worker project folder.\n\n"
        "<code>git pull</code>\n"
        "Pull the latest code from GitHub.\n\n"
        "<code>uv run python layer3/worker_prop.py</code>\n"
        "Restart the Prop Hedge MT5 worker.\n\n"
        "<b>Good result</b>\n"
        "You should see:\n\n"
        "<code>layer3.prop — MT5 connected — account=...</code>\n"
        "Confirms the worker connected to MT5.\n\n"
        "<code>layer3.prop — REP socket bound on tcp://0.0.0.0:5556</code>\n"
        "Confirms Layer 2 can request data from this worker.\n\n"
        "<code>layer3.prop — PULL socket bound on tcp://0.0.0.0:5555</code>\n"
        "Confirms this worker can receive trade tickets.\n\n"
        "<b>Important</b>\n"
        "Closing the noVNC browser tab does not stop the VPS.\n"
        "Do not close PowerShell after the worker is running."
    )


async def _cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    arg = (ctx.args[0].lower() if ctx.args else "").strip()
    if arg == "local":
        await update.message.reply_text(_update_local_text(), parse_mode="HTML")
    elif arg == "layer2":
        await update.message.reply_text(_update_layer2_text(), parse_mode="HTML")
    elif arg == "layer3":
        await update.message.reply_text(
            f"{_cmd_header('🛠️ <b>Update Layer 3 Worker</b>')}"
            "Which worker?\n\n"
            "1 — Personal Signal\n"
            "2 — Prop Hedge",
            parse_mode="HTML",
        )
        return UPDATE_LAYER3_CHOOSE
    elif arg == "account":
        await update.message.reply_text(_changeaccount_text_personal(), parse_mode="HTML")
        await update.message.reply_text(_changeaccount_text_prop(), parse_mode="HTML")
    else:
        await update.message.reply_text(_update_menu_text(), parse_mode="HTML")
    return ConversationHandler.END


async def _update_layer3_choose(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    choice = update.message.text.strip()
    if choice == "1":
        await update.message.reply_text(_update_personal_text(), parse_mode="HTML")
        return ConversationHandler.END
    if choice == "2":
        await update.message.reply_text(_update_prop_text(), parse_mode="HTML")
        return ConversationHandler.END
    await update.message.reply_text(
        "⚠️ Send <code>1</code> for Personal Signal or <code>2</code> for Prop Hedge.",
        parse_mode="HTML",
    )
    return UPDATE_LAYER3_CHOOSE


async def _update_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    await update.message.reply_text(
        f"{_cmd_header('🟡 <b>Cancelled</b>')}Update wizard aborted.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    await update.message.reply_text(
        _cmd_header("📖 <b>HedgeHog Command Menu</b>") +

        "<b>Emergency</b>\n"
        "/emergency — Force-close all positions and halt\n\n"

        "<b>Trading Control</b>\n"
        "/resume — Resume signal processing\n"
        "/stop — Halt new signals\n"
        "/rearm — Re-arm today's K1/K3 soft kills (undo /resume)\n"
        "/phase1 — Start Phase 1\n"
        "/phase2 — Start Phase 2\n\n"

        "<b>Positions &amp; Risk</b>\n"
        "/positions — Show open positions\n"
        "/equity — Account equity snapshot\n"
        "/setbaseline — Set prop risk baseline (kills + sizing)\n"
        "/setdayroll — Set prop daily-loss reset time (SGT)\n"
        "/setdeposit — Set initial deposit (equity % + fees)\n"
        "/pnl — Show P&amp;L risk dashboard\n"
        "/maxpos — Show position limit\n"
        "/setmaxpos 2 — Set position limit\n\n"

        "<b>Pair Control</b>\n"
        "/closepair EURUSD — Close and block pair\n"
        "/resumepair EURUSD — Unblock pair\n\n"

        "<b>News &amp; Suppression</b>\n"
        "/news — Upcoming high-impact events\n"
        "/blackboard — Active pair suppressions\n"
        "/consistency — Phase 2 consistency tracker\n\n"

        "<b>Configuration</b>\n"
        "/propfirm — Show prop account rules\n"
        "/changepropfirm — Update account setup\n"
        "/checkaccount — Show connected MT5 account\n"
        "/setwindow HH:MM HH:MM — Update trading window\n"
        "/cancel — Cancel active wizard\n\n"

        "<b>System</b>\n"
        "/status — Operational system state\n"
        "/health — Connectivity check\n"
        "/messages — Preview every Telegram message (page 1 of 2)\n"
        "/messages2 — Continuation (page 2 of 2)\n"
        "/update — Maintenance and deployment guide",
        parse_mode="HTML",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Telegram Message Builders
# ═════════════════════════════════════════════════════════════════════════════
# Every Telegram message text the bot ever sends lives in this section.
# logic_core.py calls these functions; it must not inline any user-facing text.
#
# Each msg_*() function returns a string (HTML-formatted). Each has a docstring
# whose first line names the message and whose "Triggered when:" line explains
# the runtime condition under which it fires — the /messages command reads these
# to build the catalog. To rewrite a message, edit ONLY the f-string body here;
# logic stays in logic_core.
# ═════════════════════════════════════════════════════════════════════════════


# Heavy-rule separator that brackets every alert header (one rule above the
# title, one below). 12 characters wide — 18 wrapped on Warren's iPhone, 12
# fits comfortably in portrait. The top rule also creates breathing room
# between the bot-name chrome and the title line.
_MSG_SEP = "━" * 12


def _msg_signed_money(value: float, currency: str = "USD") -> str:
    """Format a signed money value as '+$12.50' / '-$12.50' (sign BEFORE symbol).

    For non-USD currencies, falls through to '+SGD 12.50' / '-SGD 12.50'.
    """
    sign = "+" if value >= 0 else "-"
    mag  = abs(value)
    if (currency or "USD").upper() == "USD":
        return f"{sign}${mag:,.2f}"
    return f"{sign}{currency} {mag:,.2f}"


def _msg_positions_lines(positions: list[dict], currency: str = "USD") -> str:
    """Format a list of position dicts as plain lines (one per position).

    No account prefix, no leading indent, no 'lots'/'P&L:' filler. Used by
    msg_news_pre_close inside per-account sub-blocks. `currency` is the
    account currency of the side these positions belong to — MT5 returns
    position.profit in that currency.
    """
    if not positions:
        return "No open positions"
    out = []
    for p in positions:
        arrow = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
        pnl   = _msg_signed_money(p["profit"], currency)
        out.append(f"{p['symbol']} {arrow} {p['volume']:.2f} {pnl}")
    return "\n".join(out)


def _msg_aligned_rows(rows: list[tuple[str, str]], pad: int = 2) -> str:
    """Render a stack of (label, value) rows as 'Label: value'.

    Telegram uses a proportional font, so space-padding labels for column
    alignment never visually aligns the values — it just looks messy. A
    colon gives a consistent anchor instead.

    Drop any (label, value) where value is falsy (None/'') so callers can
    conditionally include rows. `pad` is accepted for backward compat and
    ignored.
    """
    rows = [(label, value) for label, value in rows if value not in (None, "")]
    if not rows:
        return ""
    return "\n".join(f"{label}: {value}" for label, value in rows)


def _msg_side_label(side: str) -> str:
    """'prop' → 'Prop Hedge'; anything else → 'Personal Signal'."""
    return "Prop Hedge" if side == "prop" else "Personal Signal"


def _msg_order_check_leg_line(label: str, chk: dict, currency: str = "USD") -> str:
    """Per-leg block for the pre-flight 'Signal Not Placed' alert (Issue 2).

    `currency` is the MT5 deposit currency of the worker that ran order_check
    (USD for prop, account currency for personal — e.g. SGD). Margin and free
    margin are reported by MT5 in that currency.

    Returns:
        <b>label</b>
        <status line>
        [<optional detail block, blank-line separated>]
    """
    verdict = chk.get("verdict", "?")
    if verdict == "ok":
        return f"<b>{label}</b>\n✅ Can fill"
    if verdict == "transient":
        c = chk.get("comment") or "temporarily unavailable"
        return f"<b>{label}</b>\n⚠️ {c}"
    rc      = chk.get("retcode")
    comment = (chk.get("comment") or "").strip()
    mf      = chk.get("margin_free")
    mneed   = chk.get("margin")
    if rc == 10019 or (isinstance(mf, (int, float)) and mf < 0):
        detail_lines = []
        if isinstance(mneed, (int, float)):
            detail_lines.append(f"Needs {_money(mneed, currency)} margin")
        if isinstance(mf, (int, float)):
            detail_lines.append(f"Free: {_msg_signed_money(mf, currency)}")
        if not detail_lines:
            detail_lines.append("Not enough money")
        return f"<b>{label}</b>\nREJECTED\n\n" + "\n".join(detail_lines)
    reason_map = {
        10014: "Invalid volume (lot size)",
        10015: "Invalid price",
        10016: "Invalid stops — SL/TP too close to price",
        10017: "Trading disabled on this account",
    }
    reason = reason_map.get(rc) or comment or f"order_check retcode {rc}"
    return f"<b>{label}</b>\nREJECTED\n\n{reason}"


def _msg_split_pers_amount(ticker: str, pers_value: float, usd_to_acct_rate: float) -> tuple[float, float]:
    """Recover (usd, account_ccy) from a personal figure produced by geometry."""
    rate = usd_to_acct_rate if (usd_to_acct_rate and usd_to_acct_rate > 0) else 1.0
    if ticker.endswith("USD"):
        return round(pers_value, 2), round(pers_value * rate, 2)
    return round(pers_value / rate, 2), round(pers_value, 2)


def _msg_pers_money_acct(ticker: str, pers_value: float, currency: str, rate: float,
                         signed: bool = False) -> str:
    """Personal money in the account currency MT5 reports for the personal leg.

    Geometry hands us `pers_value` either in USD (for xxxUSD pairs whose P&L
    naturally lands in USD) or already in the account currency (for non-xxxUSD
    pairs sized via tick_value, which MT5 returns in account currency).
    `_msg_split_pers_amount` recovers (usd, acct); we format only `acct`.
    """
    if (currency or "USD").upper() == "USD":
        return _money(pers_value, "USD", signed)
    _, acct = _msg_split_pers_amount(ticker, pers_value, rate)
    return _money(acct, currency, signed)


# ── Worker / system state ─────────────────────────────────────────────────

def msg_worker_offline(side: str, down_threshold_secs: int) -> str:
    """Worker OFFLINE alert.

    Triggered when: equity monitor has missed N consecutive queries to the
    Layer 3 worker (~30 s each). Sent once per outage; cleared by
    msg_worker_back_online when it next answers.
    """
    label = _msg_side_label(side)
    vps   = "#3" if side == "prop" else "#2"
    worker_file = "worker_prop.py" if side == "prop" else "worker_personal.py"
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>{label} — Worker OFFLINE</b>\n"
        f"{_MSG_SEP}\n\n"
        f"No response for ~{down_threshold_secs}s.\n"
        f"Positions may still be open.\n\n"
        f"<b>Recovery</b>\n\n"
        f"1. Open VPS {vps} noVNC\n"
        f"2. <code>cd C:/arbitrage</code>\n"
        f"3. <code>uv run python layer3/{worker_file}</code>"
    )


def msg_worker_back_online(side: str) -> str:
    """Worker recovery confirmation.

    Triggered when: equity monitor's next query succeeds after the worker was
    marked offline. Clears msg_worker_offline.
    """
    return (
        f"{_MSG_SEP}\n"
        f"✅ <b>{_msg_side_label(side)} — Worker Restored</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Worker responding normally again.\n"
        f"Execution resumed successfully."
    )


def msg_algo_trading_disabled(side: str) -> str:
    """MT5 algo-trading is off — orders would be silently rejected.

    Triggered when: equity monitor's reply reports trade_allowed=False on a
    worker that was previously enabled.
    """
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>{_msg_side_label(side)} — Algo Trading OFF</b>\n"
        f"{_MSG_SEP}\n\n"
        f"MT5 algo trading is disabled.\n"
        f"Orders will be rejected silently.\n\n"
        f"<b>Fix</b>\n\n"
        f"1. Turn Algo Trading ON (green)\n"
        f"2. Tools → Options → Expert Advisors\n"
        f"3. Uncheck:\n"
        f"   <i>“Disable algo trading when account changes”</i>"
    )


def msg_algo_trading_restored(side: str) -> str:
    """Algo-trading restored confirmation.

    Triggered when: trade_allowed flips back to True on a worker that was
    previously marked disabled.
    """
    return (
        f"{_MSG_SEP}\n"
        f"✅ <b>{_msg_side_label(side)} — Algo Trading Restored</b>\n"
        f"{_MSG_SEP}\n\n"
        f"MT5 trade_allowed restored to True.\n"
        f"Execution resumed normally."
    )


def msg_new_session_auto_resumed() -> str:
    """Daily halt cleared automatically at the prop-firm day roll.

    Triggered when: equity monitor sees the prop-firm day (11:00 SGT roll)
    has advanced past the day on which a K1/K3 daily halt was set, with the
    system not curfewed and not permanently halted.
    """
    return (
        f"{_MSG_SEP}\n"
        f"🟢 <b>New Session Started</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Daily halt cleared.\n"
        f"System armed and accepting signals."
    )


def msg_curfew_close(pos_str: str, next_open_text: str) -> str:
    """SGT curfew transition — all positions closed.

    Triggered when: equity monitor crosses the SGT curfew boundary for the
    first time today. Dispatches FORCE_CLOSE for positions only (no halt).
    """
    return (
        f"{_MSG_SEP}\n"
        f"🌙 <b>Curfew — Positions Closed</b>\n"
        f"{_MSG_SEP}\n\n"
        f"<b>Positions</b>\n\n"
        f"{pos_str}\n\n"
        f"<b>Next session</b>\n"
        f"{next_open_text}"
    )


# ── Mismatch ──────────────────────────────────────────────────────────────

def msg_mismatch_resolved(*, ticker: str, mismatch_type: str,
                          prop_dir: int | None, pers_dir: int | None,
                          post_prop_open: bool, post_pers_open: bool,
                          post_query_ok: bool) -> str:
    """Position mismatch detected and force-closed.

    Triggered when: a position mismatch (orphan or same-direction) persists
    on both accounts ≥120 s past first detection. The offending leg(s) are
    force-closed before this alert is sent.
    """
    _dir = {0: "LONG", 1: "SHORT"}
    if mismatch_type == "prop_only":
        problem_block = (
            f"<b>Orphan detected</b>\n"
            f"{_dir.get(prop_dir, '?')} on Prop Hedge\n"
            f"(no Personal Signal match)"
        )
        action_line = "Force-closed Prop Hedge leg"
    elif mismatch_type == "pers_only":
        problem_block = (
            f"<b>Orphan detected</b>\n"
            f"{_dir.get(pers_dir, '?')} on Personal Signal\n"
            f"(no Prop Hedge match)"
        )
        action_line = "Force-closed Personal Signal leg"
    else:  # same_direction
        problem_block = (
            f"<b>Hedge broken</b>\n"
            f"Both accounts hold {_dir.get(prop_dir, '?')}\n"
            f"(direction mismatch)"
        )
        action_line = "Force-closed both legs"

    if not post_query_ok:
        pers_state = "QUERY FAILED"
        prop_state = "QUERY FAILED"
        status     = "⚠️ Could not verify — check MT5."
    else:
        pers_state = "OPEN" if post_pers_open else "FLAT"
        prop_state = "OPEN" if post_prop_open else "FLAT"
        if not post_prop_open and not post_pers_open:
            status = "✅ Sync restored."
        else:
            status = "⚠️ Action required — check MT5 immediately."

    final_state = _msg_aligned_rows([
        ("Personal Signal", pers_state),
        ("Prop Hedge",      prop_state),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>Position Mismatch — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{problem_block}\n\n"
        f"<b>Action</b>\n"
        f"{action_line}\n\n"
        f"<b>Final State</b>\n"
        f"{final_state}\n\n"
        f"{status}"
    )


def msg_news_window_cleared(expired_pairs: list[tuple[str, str]]) -> str:
    """News suppression windows have expired for one or more pairs.

    Triggered when: pre-close monitor finds suppression entries whose end
    time has passed. NEWS_CLEAR is sent to Layer 3 right after this alert.
    expired_pairs is a list of (ticker, end_time_sgt_str) tuples.
    """
    pair_blocks = "\n\n".join(
        f"<b>{ticker}</b> window expired\n(until {end_sgt})"
        for ticker, end_sgt in expired_pairs
    )
    return (
        f"{_MSG_SEP}\n"
        f"🟢 <b>News Window Cleared</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{pair_blocks}\n\n"
        f"Signals accepted again."
    )


def msg_news_pre_close(*, currency: str, event_title: str,
                       event_time_sgt: str, mins_to_event: int,
                       affected_pers: list[dict], affected_prop: list[dict],
                       suppression_end_sgt: str,
                       pers_currency: str = "USD") -> str:
    """High-impact news event entered the ban zone — positions are closing.

    Triggered when: a ForexFactory high-impact event for one of the tracked
    currencies is within ±NEWS_TRADING_BAN_WINDOW minutes of now. Fires once
    per (ticker, event_time) pair; affected positions are force-closed and
    new signals on those pairs are blocked until ban_window_min after the event.

    `currency` is the news event's currency (USD/GBP/EUR/etc.). `pers_currency`
    is the personal MT5 account currency used to format personal-side P&L
    (SGD on the live Fusion account). Prop stays USD.
    """
    if mins_to_event >= 0:
        time_detail = f"{event_time_sgt} (in {mins_to_event} min)"
    else:
        time_detail = f"{event_time_sgt} ({abs(mins_to_event)} min ago)"

    return (
        f"{_MSG_SEP}\n"
        f"📰 <b>News Pre-Close — {currency}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"<b>{currency} {event_title}</b>\n"
        f"{time_detail}\n\n"
        f"<b>Closing Positions</b>\n\n"
        f"<b>Personal Signal</b>\n"
        f"{_msg_positions_lines(affected_pers, pers_currency)}\n\n"
        f"<b>Prop Hedge</b>\n"
        f"{_msg_positions_lines(affected_prop, 'USD')}\n\n"
        f"<b>Signals blocked until</b>\n"
        f"{suppression_end_sgt}"
    )


# ── Kill conditions ──────────────────────────────────────────────────────

def msg_phase1_stage_reached(prop_equity: float, stage_value: float,
                             next_resume_text: str) -> str:
    """Phase 1 stage reached — profitable day locked.

    Triggered when: in Phase 1, prop equity ≥ the active stage value.
    Positions are force-closed; system auto-resumes next session aiming
    at the next stage. NOT permanent.
    """
    rows = _msg_aligned_rows([
        ("Equity", f"${prop_equity:,.2f}"),
        ("Stage",  f"${stage_value:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🎯 <b>Phase 1 — Stage Reached</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"Profitable day locked.\n"
        f"Positions force-closed.\n\n"
        f"<b>Auto-resume</b>\n"
        f"{next_resume_text}"
    )


def msg_kill1_phase1(prop_equity: float, daily_floor: float, day_start: float,
                     next_resume_text: str) -> str:
    """KILL 1 — daily loss limit hit during Phase 1.

    Triggered when: in Phase 1, prop equity ≤ day_start × (1 - dd_daily_pct/100).
    Soft kill: system halts for the day and auto-resumes next session.
    Suppressed by same-day /resume override.
    """
    rows = _msg_aligned_rows([
        ("Equity",      f"${prop_equity:,.2f}"),
        ("Daily floor", f"${daily_floor:,.2f}"),
        ("Day-start",   f"${day_start:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🔴 <b>KILL 1 — Daily Loss Limit</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"All positions closed.\n"
        f"Trading halted for today.\n\n"
        f"<b>Auto-resume</b>\n{next_resume_text}\n\n"
        f"/resume to restart now"
    )


def msg_kill2_phase1(prop_equity: float, overall_floor: float) -> str:
    """KILL 2 — overall drawdown limit hit during Phase 1 (permanent).

    Triggered when: in Phase 1, prop equity ≤ baseline - (baseline × dd_overall_pct).
    Permanent halt: requires /changepropfirm → /phase1 → /resume to restart.
    """
    rows = _msg_aligned_rows([
        ("Equity", f"${prop_equity:,.2f}"),
        ("Floor",  f"${overall_floor:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🔴 <b>KILL 2 — Max Drawdown Hit</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"All positions closed.\n"
        f"Permanent halt.\n\n"
        f"<b>Next</b>\n"
        f"/changepropfirm\n"
        f"/phase1\n"
        f"/resume"
    )


def msg_kill4_phase1_passed(prop_equity: float) -> str:
    """KILL 4 — Phase 1 evaluation passed.

    Triggered when: in Phase 1, prop equity ≥ funded-line stage value.
    Reported via the phase1_strategy decision path.
    """
    rows = _msg_aligned_rows([
        ("Equity", f"${prop_equity:,.2f}"),
        ("Target", "≥ funded line"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🏆 <b>Phase 1 PASSED</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"All positions closed.\n"
        f"System halted.\n\n"
        f"/phase2\n"
        f"to start funded phase"
    )


def msg_kill2_phase2plus(prop_equity: float, overall_floor: float,
                         dd_overall_pct: float, baseline: float, pos_str: str) -> str:
    """KILL 2 — overall drawdown limit hit in Phase 2+ (permanent).

    Triggered when: in Phase 2 or later, prop equity ≤ baseline -
    (baseline × dd_overall_pct). System permanently halted.
    """
    rows = _msg_aligned_rows([
        ("Equity",     f"${prop_equity:,.2f}"),
        ("Floor",      f"${overall_floor:,.2f}"),
        ("Overall DD", f"{dd_overall_pct:.1f}%"),
        ("Baseline",   f"${baseline:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🔴 <b>KILL 2 — Max Drawdown Hit</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"<b>Positions</b>\n\n"
        f"{pos_str}\n\n"
        f"All positions closed.\n"
        f"Permanent halt.\n\n"
        f"<b>Next</b>\n"
        f"/changepropfirm\n"
        f"/phase1"
    )


def msg_kill1_phase2plus(prop_equity: float, daily_floor: float, day_start: float,
                         daily_loss_amt: float, dd_daily_pct: float,
                         pos_str: str, overall_floor: float,
                         next_resume_text: str) -> str:
    """KILL 1 — daily loss limit hit in Phase 2+.

    Triggered when: in Phase 2+, prop equity ≤ day_start - daily_loss_amt
    where daily_loss_amt = day_start × dd_daily_pct/100. Soft kill — system
    auto-resumes next session. Suppressed by same-day /resume override.
    """
    rows = _msg_aligned_rows([
        ("Equity",            f"${prop_equity:,.2f}"),
        ("Daily floor",       f"${daily_floor:,.2f}"),
        ("Day-start",         f"${day_start:,.2f}"),
        ("Max loss",          f"${daily_loss_amt:,.2f} ({dd_daily_pct:.1f}%)"),
        ("Overall DD floor",  f"${overall_floor:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🔴 <b>KILL 1 — Daily Loss Limit</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"<b>Positions</b>\n\n"
        f"{pos_str}\n\n"
        f"All positions closed.\n"
        f"Trading halted for today.\n\n"
        f"<b>Auto-resume</b>\n{next_resume_text}\n\n"
        f"/resume to restart\n"
        f"/changepropfirm to switch challenge"
    )


def msg_kill3_daily_profit_cap(prop_equity: float, daily_cap_level: float,
                               day_start: float, layer_cap_amt: float,
                               pos_str: str, next_resume_text: str) -> str:
    """KILL 3 — daily profit cap hit (protects consistency rule).

    Triggered when: prop equity ≥ day_start + (baseline × daily_profit_cap_pct).
    Soft kill — system auto-resumes next session. Suppressed by same-day
    /resume override.
    """
    rows = _msg_aligned_rows([
        ("Equity",    f"${prop_equity:,.2f}"),
        ("Cap level", f"${daily_cap_level:,.2f}"),
        ("Day-start", f"${day_start:,.2f}"),
        ("Cap",       f"+${layer_cap_amt:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🟡 <b>KILL 3 — Daily Profit Cap</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"<b>Positions</b>\n\n"
        f"{pos_str}\n\n"
        f"All positions closed.\n"
        f"Trading halted for today.\n\n"
        f"<b>Auto-resume</b>\n{next_resume_text}\n\n"
        f"/resume to restart"
    )


def msg_kill4_phase1_via_target(prop_equity: float, overall_pct: float,
                                pos_str: str) -> str:
    """KILL 4 — Phase 1 evaluation passed (via profit-target branch).

    Triggered when: in Phase 1, (equity-baseline)/baseline ≥ profit_target_pct.
    Same outcome as msg_kill4_phase1_passed but reached via the unified K4
    branch with the position snapshot included.
    """
    rows = _msg_aligned_rows([
        ("Profit", f"{overall_pct:.1f}% ≥ target"),
        ("Equity", f"${prop_equity:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🏆 <b>Phase 1 PASSED</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"<b>Positions</b>\n\n"
        f"{pos_str}\n\n"
        f"All positions closed.\n"
        f"System halted.\n\n"
        f"/phase2 to start funded phase\n"
        f"/changepropfirm for new challenge"
    )


def msg_kill4_phase2plus(phase: int, prop_equity: float, overall_pct: float,
                         pos_str: str) -> str:
    """KILL 4 — profit target reached in Phase 2+ (permanent halt).

    Triggered when: in Phase 2 or later, (equity-baseline)/baseline ≥
    profit_target_pct. Permanent halt — user must /phase2 or /stop.
    """
    rows = _msg_aligned_rows([
        ("Profit", f"{overall_pct:.1f}% ≥ target"),
        ("Equity", f"${prop_equity:,.2f}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🏆 <b>Phase {phase} Target Reached</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{rows}\n\n"
        f"<b>Positions</b>\n\n"
        f"{pos_str}\n\n"
        f"All positions closed.\n"
        f"System halted.\n\n"
        f"/phase2 to start new cycle\n"
        f"/stop to end trading"
    )


def msg_kill5_consistency() -> str:
    """KILL 5 — Phase 2 consistency rule met (permanent).

    Triggered when: in Phase 2, the largest single profitable day is below
    consistency_threshold_pct of total profit, meaning the rule is satisfied
    and withdrawal is safe. Permanent halt — submit profit-share claim, then
    /phase2 + /resume to start a new cycle.

    Detail data (table, overall %, positions) intentionally not shown here —
    /consistency, /pnl and /positions surface it on demand.
    """
    return (
        f"{_MSG_SEP}\n"
        f"🏆 <b>Consistency Rule Met</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Consistency requirement satisfied.\n\n"
        f"Eligible for payout processing.\n\n"
        f"System halted.\n\n"
        f"<b>Next</b>\n"
        f"• Withdraw payout\n"
        f"• Start new cycle"
    )


# ── Trade Open / Close ────────────────────────────────────────────────────

def msg_trade_opened(*, ticker: str, phase: int,
                     phase_context_extra: str,
                     pers_arrow: str, pers_lots: float,
                     pers_entry_fmt: str, pers_sl_fmt: str, pers_tp_fmt: str,
                     pers_dollar_risk: float, pers_reward: float, pers_rr: float,
                     pers_ticket: object, pers_currency: str, pers_usd_to_acct_rate: float,
                     prop_arrow: str, prop_lots: float,
                     prop_entry_fmt: str, prop_sl_fmt: str, prop_tp_fmt: str,
                     prop_dollar_risk: float, prop_reward: float, prop_rr: float,
                     prop_ticket: object) -> str:
    """Trade Opened — both legs filled successfully.

    Triggered when: after _verify_and_notify polls Layer 3 and both prop and
    personal report status=FILLED. Personal Risk/Reward render in the MT5
    account currency (SGD for the live personal account); prop in USD. Prices
    (Entry/SL/TP) are raw forex quotes — no currency symbol.
    """
    pers_levels = _msg_aligned_rows([
        ("Size",  f"{pers_lots:.2f} lots"),
        ("Entry", pers_entry_fmt),
        ("SL",    pers_sl_fmt),
        ("TP",    pers_tp_fmt),
    ])
    pers_risk_block = _msg_aligned_rows([
        ("Risk",   _msg_pers_money_acct(ticker, pers_dollar_risk, pers_currency, pers_usd_to_acct_rate)),
        ("Reward", _msg_pers_money_acct(ticker, pers_reward,      pers_currency, pers_usd_to_acct_rate)),
        ("RR",     f"{pers_rr:.2f}"),
    ])
    pers_ticket_row = _msg_aligned_rows([("Ticket", f"#{pers_ticket}")])

    prop_levels = _msg_aligned_rows([
        ("Size",  f"{prop_lots:.2f} lots"),
        ("Entry", prop_entry_fmt),
        ("SL",    prop_sl_fmt),
        ("TP",    prop_tp_fmt),
    ])
    prop_risk_block = _msg_aligned_rows([
        ("Risk",   f"${prop_dollar_risk:,.2f}"),
        ("Reward", f"${prop_reward:,.2f}"),
        ("RR",     f"{prop_rr:.2f}"),
    ])
    prop_ticket_row = _msg_aligned_rows([("Ticket", f"#{prop_ticket}")])

    return (
        f"{_MSG_SEP}\n"
        f"🟢 <b>{ticker} — Trade Opened</b>\n"
        f"{_MSG_SEP}\n\n"
        f"<b>Personal Signal</b>  {pers_arrow}\n"
        f"{pers_ticket_row}\n\n"
        f"{pers_levels}\n\n"
        f"{pers_risk_block}\n\n"
        f"<b>Prop Hedge</b>  {prop_arrow}\n"
        f"{prop_ticket_row}\n\n"
        f"{prop_levels}\n\n"
        f"{prop_risk_block}\n\n"
        f"<b>Context</b>\n"
        f"Phase {phase}"
        + (f"\n{phase_context_extra}" if phase_context_extra else "")
    )


def msg_position_closed(*, symbol: str,
                        pers_pos_data: dict | None, prop_pos_data: dict | None,
                        pers_deal: dict | None, prop_deal: dict | None,
                        curr_pers: list[dict], curr_prop: list[dict],
                        pers_currency: str, pers_eq_str: str, prop_eq_str: str,
                        is_news_close: bool, account_mode: str) -> str:
    """Position Closed — TP / SL / News / Other close for one symbol.

    Triggered when: the equity monitor's _detect_closes() flushes a pending
    close. Flush happens AS SOON AS both sides report deals via MT5
    history_deals_get — so Trade P&L / Commission match the trade journal
    byte-for-byte. The `(est.)` fallback path only fires when MT5 history
    didn't surface a deal within `_CLOSE_DEAL_TIMEOUT` (10 min), which is
    the broker-side outlier — typically MetaQuotes Demo accounts under
    their 2-3h indexing lag.

    Layout mirrors Trade Opened: aligned label rows per side, four
    close-type emojis (🟢 TP / 🔴 SL / 📰 News / ⚠️ Other). Account mode
    (demo/real) is inferred from Layer 3's deal reply.
    """
    def _reason_label(deal: dict | None) -> str:
        if is_news_close:
            return "NEWS"
        if deal and deal.get("found") and deal.get("close_reason"):
            return deal["close_reason"]
        return "—"

    pers_reason = _reason_label(pers_deal)
    prop_reason = _reason_label(prop_deal)

    # ── Title ────────────────────────────────────────────────────────────
    if is_news_close:
        title = f"📰 <b>{symbol} — News Close</b>"
    elif pers_pos_data:
        if pers_deal and pers_deal.get("found"):
            cr = pers_deal["close_reason"]
            if cr == "TP":
                title = f"🟢 <b>{symbol} — Take Profit</b>"
            elif cr == "SL":
                title = f"🔴 <b>{symbol} — Stop Loss</b>"
            else:
                title = f"⚠️ <b>{symbol} — Position Closed</b>"
        else:
            pers_pnl = pers_pos_data["profit"]
            title = (
                f"🟢 <b>{symbol} — Take Profit</b>" if pers_pnl >= 0
                else f"🔴 <b>{symbol} — Stop Loss</b>"
            )
    else:
        title = f"⚠️ <b>{symbol} — Position Closed</b>"

    # ── Per-side block ───────────────────────────────────────────────────
    def _side_block(label: str, pos_data: dict | None, deal: dict | None,
                    reason_label: str, currency: str) -> str:
        if not pos_data:
            return f"<b>{label}</b>\nNo matching position — already closed"
        dir_str = "↑ LONG" if pos_data["type"] == 0 else "↓ SHORT"
        if deal and deal.get("found"):
            pnl_val    = deal["net_pnl"]
            exit_price = _fmt_price(symbol, deal["close_price"])
            # Trading Fee = ALL broker costs on this trade (commission + swap),
            # not just the commission field. Net Trade P&L already has it baked in.
            fee_val    = deal.get("commission", 0.0) + deal.get("swap", 0.0)
            pnl_str    = _msg_signed_money(pnl_val, currency)
            fee_str    = _msg_signed_money(fee_val, currency) if fee_val else ""
        else:
            pnl_val = pos_data["profit"]
            exit_price = (
                _fmt_price(symbol, pos_data["tp"]) if pnl_val >= 0
                else _fmt_price(symbol, pos_data["sl"])
            )
            pnl_str    = f"{_msg_signed_money(pnl_val, currency)} (est.)"
            fee_str    = ""   # deal missing → no fee row (and P&L is only an estimate)

        levels = _msg_aligned_rows([
            ("Size",  f"{pos_data['volume']:.2f} lots"),
            ("Entry", _fmt_price(symbol, pos_data['price_open'])),
            ("Exit",  exit_price),
        ])
        outcome = _msg_aligned_rows([
            ("Reason",      reason_label),
            ("Trade P&amp;L",   pnl_str),
            ("Trading Fee", fee_str),  # empty rows dropped by helper
        ])
        ticket_row = (
            _msg_aligned_rows([("Ticket", f"#{pos_data['ticket']}")])
            if pos_data.get("ticket") else ""
        )
        header = f"<b>{label}</b>  {dir_str}"
        if ticket_row:
            header += f"\n{ticket_row}"
        return (
            f"{header}\n\n"
            f"{levels}\n\n"
            f"{outcome}"
        )

    pers_block = _side_block("Personal Signal", pers_pos_data, pers_deal, pers_reason, pers_currency)
    prop_block = _side_block("Prop Hedge",      prop_pos_data, prop_deal, prop_reason, "USD")

    # ── After Close ──────────────────────────────────────────────────────
    def _pos_summary(pos_list: list[dict]) -> str:
        if not pos_list:
            return "No open positions"
        return "\n".join(
            f"{p['symbol']} {'↑ LONG' if p['type'] == 0 else '↓ SHORT'} {p['volume']:.2f} lots"
            for p in pos_list
        )

    after_block = (
        f"<b>After Close</b>\n\n"
        f"<b>Personal Signal</b>\n{_pos_summary(curr_pers)}\n\n"
        f"<b>Prop Hedge</b>\n{_pos_summary(curr_prop)}"
    )

    # ── Account Equity ───────────────────────────────────────────────────
    equity_block = (
        f"<b>Account Equity</b>\n"
        f"Personal Signal: {pers_eq_str}\n"
        f"Prop Hedge: {prop_eq_str}"
    )

    # ── Footer for missing deal data ─────────────────────────────────────
    pers_deal_missing = pers_pos_data and not (pers_deal and pers_deal.get("found"))
    prop_deal_missing = prop_pos_data and not (prop_deal and prop_deal.get("found"))
    footer = ""
    if pers_deal_missing or prop_deal_missing:
        if account_mode == "demo":
            footer = "ℹ️ Demo account — exact figures sync to journal in ~2-3h."
        elif account_mode == "real":
            footer = "⚠️ Deal data unavailable — check journal shortly."

    sections = [
        f"{_MSG_SEP}\n{title}\n{_MSG_SEP}",
        pers_block,
        prop_block,
        after_block,
        equity_block,
    ]
    if footer:
        sections.append(footer)
    return "\n\n".join(sections)


def msg_signal_not_placed_terminal(*, ticker: str,
                                   pers_status: dict, prop_status: dict,
                                   pers_arrow: str,
                                   entry_fmt: str, pers_sl_fmt: str, pers_tp_fmt: str) -> str:
    """Signal Not Placed — at least one leg returned an immediate terminal error.

    Triggered when: _verify_and_notify's first status query returns
    REJECTED / ERROR / UNSUPPORTED_LIMIT_SETUP on either side, before any
    fill. Order_check pre-flight should have caught most of these.
    """
    def _side_block(s: dict, label: str) -> str:
        st  = s.get("status", "UNKNOWN")
        err = s.get("error") or s.get("broker_comment") or ""
        if err:
            return f"<b>{label}</b>\n{st}\n{err}"
        return f"<b>{label}</b>\n{st}"

    signal_rows = _msg_aligned_rows([
        ("Entry", entry_fmt),
        ("SL",    pers_sl_fmt),
        ("TP",    pers_tp_fmt),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🚫 <b>Signal Not Placed — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{_side_block(pers_status, 'Personal Signal')}\n\n"
        f"{_side_block(prop_status, 'Prop Hedge')}\n\n"
        f"<b>Signal</b>\n"
        f"{pers_arrow}\n\n"
        f"{signal_rows}"
    )


def msg_signal_not_placed_preflight(*, ticker: str,
                                    pers_chk: dict, prop_chk: dict,
                                    pers_arrow: str,
                                    entry_fmt: str, pers_sl_fmt: str, pers_tp_fmt: str,
                                    pers_currency: str = "USD") -> str:
    """Signal Not Placed — pre-flight order_check rejected one leg.

    Triggered when: order_check on at least one leg returned verdict=reject
    BEFORE either order was sent (Issue 2 guard). Prevents orphaned trades.
    `pers_currency` is the personal MT5 account currency so margin/free are
    labeled correctly (SGD for personal, USD for prop).
    """
    signal_rows = _msg_aligned_rows([
        ("Entry", entry_fmt),
        ("SL",    pers_sl_fmt),
        ("TP",    pers_tp_fmt),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"🚫 <b>Signal Not Placed — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"One leg cannot fill,\n"
        f"so no order was placed.\n\n"
        f"<i>(prevents orphan trades\nand wasted commissions)</i>\n\n"
        f"{_msg_order_check_leg_line('Personal Signal', pers_chk, pers_currency)}\n\n"
        f"{_msg_order_check_leg_line('Prop Hedge', prop_chk, 'USD')}\n\n"
        f"<b>Signal</b>\n"
        f"{pers_arrow}\n\n"
        f"{signal_rows}"
    )


def msg_order_not_filled(*, ticker: str, resting: bool,
                         pers_final: dict | None, prop_final: dict | None,
                         entry: float,
                         pers_arrow: str, pers_sl_fmt: str, pers_tp_fmt: str,
                         pers_lots: float, prop_lots: float) -> str:
    """Order Not Filled / Limit Order Resting — non-success outcome.

    Triggered when: at least one leg failed to reach FILLED within the poll
    horizon. If at least one side rests as PENDING_PLACED (market closed,
    limit dropped) and no hard error elsewhere, the title becomes "Limit
    Order Resting"; otherwise "Order Not Filled".

    `pers_final` / `prop_final` are the raw order-status dicts from Layer 3
    (or None if no confirmation arrived). All leg/ticket text is built here.
    """
    def _leg_block(s: dict | None, label: str) -> str:
        if s is None:
            return f"<b>{label}</b>\n⚠️ No confirmation received"
        st     = s.get("status", "UNKNOWN")
        reason = s.get("broker_comment") or s.get("error") or ""
        if st == "FILLED":
            fill = _fmt_price(ticker, s.get("actual_fill_price", entry))
            return f"<b>{label}</b>\n✅ Filled @ {fill}"
        if st == "PENDING_PLACED":
            px = _fmt_price(ticker, s.get("requested_entry", entry))
            return (
                f"<b>{label}</b>\n"
                f"⏳ Limit @ {px}\n"
                f"<i>(market was closed)</i>"
            )
        if st == "UNSUPPORTED_LIMIT_SETUP":
            base = f"<b>{label}</b>\n🚫 {st}"
            return f"{base}\n{reason}" if reason else base
        base = f"<b>{label}</b>\n❌ {st}"
        return f"{base}\n{reason}" if reason else base

    pers_block = _leg_block(pers_final, "Personal Signal")
    prop_block = _leg_block(prop_final, "Prop Hedge")

    # Collect tickets that landed (filled or resting). Show whichever exist.
    tickets: list[tuple[str, object]] = []
    if pers_final and pers_final.get("mt5_order_ticket"):
        tickets.append(("Personal", pers_final["mt5_order_ticket"]))
    if prop_final and prop_final.get("mt5_order_ticket"):
        tickets.append(("Prop", prop_final["mt5_order_ticket"]))
    if not tickets:
        ticket_block = ""
    elif len(tickets) == 1:
        ticket_block = f"\n\n<b>Ticket</b>\n#{tickets[0][1]}"
    else:
        ticket_lines = "\n".join(f"{side}: #{num}" for side, num in tickets)
        ticket_block = f"\n\n<b>Tickets</b>\n{ticket_lines}"

    header = (f"⏳ <b>Limit Order Resting — {ticker}</b>" if resting
              else f"⚠️ <b>Order Not Filled — {ticker}</b>")

    signal_rows = _msg_aligned_rows([
        ("Entry", _fmt_price(ticker, entry)),
        ("SL",    pers_sl_fmt),
        ("TP",    pers_tp_fmt),
    ])
    lots_rows = (
        f"Personal {pers_lots:.2f}\n"
        f"Prop {prop_lots:.2f}"
    )
    return (
        f"{_MSG_SEP}\n"
        f"{header}\n"
        f"{_MSG_SEP}\n\n"
        f"{pers_block}\n\n"
        f"{prop_block}"
        f"{ticket_block}\n\n"
        f"<b>Signal</b>\n\n"
        f"{pers_arrow}\n\n"
        f"{signal_rows}\n\n"
        f"<b>Lots</b>\n"
        f"{lots_rows}"
    )


# ── Signal block / skip ──────────────────────────────────────────────────

def msg_signal_blocked_p_halt(ticker: str, signal: str) -> str:
    """Signal blocked — system permanently halted (K2/K4/K5).

    Triggered when: a TradingView signal arrives while permanently_halted is
    set. Dedup'd via _maybe_block_alert to one per (ticker, p_halt) per 30 min.
    """
    return (
        f"{_MSG_SEP}\n"
        f"🔴 <b>Signal Blocked — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"System permanently halted.\n"
        f"<i>(K2 / K4 / K5 triggered)</i>\n\n"
        f"<b>Signal</b>\n{signal}\n\n"
        f"<b>Recovery</b>\n"
        f"/phase2\n"
        f"/changepropfirm\n"
        f"/resume"
    )


def msg_signal_skipped_halted(ticker: str, signal: str) -> str:
    """Signal skipped — system halted (daily halt or manual /stop).

    Triggered when: a TradingView signal arrives while active=False (K1/K3
    daily halt or user issued /stop). Dedup'd to 30 min per ticker.
    """
    return (
        f"{_MSG_SEP}\n"
        f"⏸️ <b>Signal Skipped — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"System halted.\n"
        f"<i>(K1 / K3 / manual /stop)</i>\n\n"
        f"<b>Signal</b>\n{signal}\n\n"
        f"<b>Auto-resume</b>\nNext session\n\n"
        f"/resume to restart now"
    )


def msg_signal_suppressed(ticker: str, signal: str, reason: str) -> str:
    """Signal suppressed — pair under news ban or manual /closepair block.

    Triggered when: a TradingView signal arrives for a pair currently in
    _news_suppressed_pairs (Phase 2+) or _manual_suppressed_pairs.
    Dedup'd to 30 min per (ticker, reason).
    """
    return (
        f"{_MSG_SEP}\n"
        f"📰 <b>Signal Suppressed — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"<b>Reason</b>\n{reason}\n\n"
        f"<b>Signal</b>\n{signal}\n\n"
        f"Trading resumes automatically\n"
        f"when the window expires."
    )


def msg_signal_skipped_max_pos(ticker: str, signal: str,
                               open_count: int, max_pos: int) -> str:
    """Signal skipped — open position cap reached.

    Triggered when: a TradingView signal arrives but the prop account
    already holds ≥ max_open_positions positions.
    """
    return (
        f"{_MSG_SEP}\n"
        f"🚫 <b>Signal Skipped — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Max open positions reached.\n"
        f"<i>({open_count} / {max_pos})</i>\n\n"
        f"<b>Signal</b>\n{signal}\n\n"
        f"/setmaxpos N\n"
        f"to increase the limit"
    )


def msg_signal_skipped_already_open(ticker: str, signal: str) -> str:
    """Signal skipped — this pair already has a bot-opened position.

    Triggered when: a TradingView signal arrives for a pair the prop account
    already holds an open position on. With multiple indicators able to fire
    the same pair, this prevents a second (duplicate) position being stacked
    on top of the first. Dedup'd to 30 min per ticker.
    """
    return (
        f"{_MSG_SEP}\n"
        f"🚫 <b>Signal Skipped — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Position already open on this pair.\n"
        f"<i>(duplicate signal dropped)</i>\n\n"
        f"<b>Signal</b>\n{signal}\n\n"
        f"Waits until the current\n"
        f"{ticker} trade closes."
    )


def msg_signal_blocked_algo_disabled(ticker: str, side: str) -> str:
    """Signal blocked — algo trading disabled on one of the workers.

    Triggered when: at the moment of signal processing, the worker's
    /equity reply reports trade_allowed=False. Differs from the equity
    monitor's standing alert in that this one blocks the live trade.
    """
    label = _msg_side_label(side)
    return (
        f"{_MSG_SEP}\n"
        f"🚫 <b>Signal Blocked — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{label} algo trading\n"
        f"is <b>DISABLED</b>.\n\n"
        f"<b>Fix</b>\n\n"
        f"1. Turn Algo Trading ON (green)\n\n"
        f"2. Tools → Options\n"
        f"   → Expert Advisors\n\n"
        f"3. Uncheck:\n"
        f"   <i>“Disable algo trading\n"
        f"   when account changes”</i>"
    )


def msg_signal_blocked_generic(ticker: str, *, main: str,
                               command: str | None = None,
                               tail: str | None = None) -> str:
    """Signal blocked — structured 'main + optional /command + optional tail'.

    Triggered when: a config-side error makes the signal unsizable. Two
    call patterns:
      • Phase 1 not configured → main="Phase 1 not configured.",
        command="/phase1", tail="to set reward:risk first."
      • Phase 1 live equity unavailable → main only, no command/tail.
    """
    body = main
    if command and tail:
        body = f"{main}\n\n<b>Run</b>\n{command}\n\n{tail}"
    elif command:
        body = f"{main}\n\n<b>Run</b>\n{command}"
    return (
        f"{_MSG_SEP}\n"
        f"🚫 <b>Signal Blocked — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{body}"
    )


def msg_geometry_reject(ticker: str, phase: int, reject_reason: str, signal: str) -> str:
    """Signal Skipped — phase strategy refused to size the trade.

    Triggered when: phase1_strategy or phase2_strategy returns a 'reject'
    key — e.g. SL too tight, lots below broker minimum, max_prop_lots cap hit.
    """
    return (
        f"{_MSG_SEP}\n"
        f"🚫 <b>Signal Skipped — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Phase {phase} sizing rejected.\n\n"
        f"{reject_reason}\n\n"
        f"<b>Signal</b>\n{signal}"
    )


def msg_internal_error(ticker: str, exc: object) -> str:
    """Internal Error — _verify_and_notify crashed.

    Triggered when: the order-confirmation task raises before producing a
    Trade Opened / Not Filled alert. Positions MAY be open; user must check
    MT5 manually.
    """
    # Split the exception representation so the type sits on its own line.
    if isinstance(exc, BaseException):
        exc_str = f"{type(exc).__name__}: {exc}"
    else:
        exc_str = str(exc)
    if ":" in exc_str:
        head, _, tail = exc_str.partition(": ")
        exc_block = f"{head}:\n{tail}"
    else:
        exc_block = exc_str
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>Internal Error — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Order confirmation task crashed.\n\n"
        f"{exc_block}\n\n"
        f"Check VPS #1 logs.\n"
        f"Positions may still be open.\n\n"
        f"Verify MT5 manually."
    )


# ── Plain diagnostic strings (sent as raw text, not HTML-formatted) ──────

def msg_contract_query_failed(side: str, exc: object) -> str:
    """Diagnostic — Layer 3 /equity query failed at signal-processing time.

    Triggered when: _query_equity to either worker throws while sizing a
    fresh signal. Logged as ERROR and returned to TradingView as 503.
    """
    label = _msg_side_label(side)
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>Contract Query Failed</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{label} contract query failed.\n\n"
        f"{exc}"
    )


def msg_baseline_missing() -> str:
    """Diagnostic — baseline_equity not set.

    Triggered when: a signal arrives before /phase1 or /phase2 has set
    baseline_equity. The bot refuses to size without a static baseline.
    """
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>Baseline Missing</b>\n"
        f"{_MSG_SEP}\n\n"
        f"baseline_equity not set.\n\n"
        f"<b>Run</b>\n"
        f"/phase1\n"
        f"or\n"
        f"/phase2\n\n"
        f"via Telegram first."
    )


def msg_invalid_contract_data(ticker: str, tick_size: float, tick_value: float) -> str:
    """Diagnostic — prop worker returned invalid contract data.

    Triggered when: trade_tick_size or trade_tick_value from Layer 3 is
    ≤ 0 for the signal's ticker. Means MT5 has no live contract for it.
    """
    rows = _msg_aligned_rows([
        ("tick_size",  f"{tick_size}"),
        ("tick_value", f"{tick_value}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>Invalid Contract Data</b>\n"
        f"{_MSG_SEP}\n\n"
        f"Invalid contract data\n"
        f"returned from prop worker.\n\n"
        f"<b>{ticker}</b>\n\n"
        f"{rows}"
    )


def msg_tp_distance_zero(ticker: str, tp: float, entry: float) -> str:
    """Diagnostic — TP equals entry, so TP distance is zero.

    Triggered when: the signal's TP and entry are identical. Sizing would
    divide by zero; signal is rejected with 422.
    """
    rows = _msg_aligned_rows([
        ("tp",    f"{tp}"),
        ("entry", f"{entry}"),
    ])
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>TP Distance Zero — {ticker}</b>\n"
        f"{_MSG_SEP}\n\n"
        f"TP distance is zero.\n\n"
        f"{rows}"
    )


def msg_dispatch_failed(side: str, exc: object) -> str:
    """Diagnostic — ZMQ push of ticket to a Layer 3 worker failed.

    Triggered when: _push_ticket to prop or personal ZMQ_PUSH socket
    throws. Logged as ERROR; the partial signal may have already gone
    to the other leg, so user should verify manually.
    """
    label = _msg_side_label(side)
    return (
        f"{_MSG_SEP}\n"
        f"⚠️ <b>Dispatch Failed</b>\n"
        f"{_MSG_SEP}\n\n"
        f"{label} dispatch failed.\n\n"
        f"{exc}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Message Catalog + /messages command
# ═════════════════════════════════════════════════════════════════════════════
# MESSAGE_CATALOG is the list /messages walks through. Each entry is:
#   (short_name, trigger_description, render_demo_callable)
# render_demo_callable() returns a fully-rendered preview using realistic
# stand-in arguments — what Warren will see on his phone when he runs
# /messages, so he can decide which to redesign.

def _demo_pos_str() -> str:
    """Demo snapshot that mirrors live rendering — personal in SGD, prop in USD."""
    return (
        "Personal: EURUSD ↑ LONG 0.10 -SGD 12.50\n"
        "Prop: XAUUSD ↓ SHORT 0.05 +$34.20"
    )


def _demo_close_pos() -> dict:
    return {
        "symbol": "EURUSD", "type": 0, "volume": 0.10,
        "price_open": 1.08234, "tp": 1.08600, "sl": 1.07900,
        "profit": -12.50, "ticket": 123456789,
    }


def _demo_close_deal_found() -> dict:
    return {
        "found": True, "net_pnl": -12.50, "close_price": 1.07900,
        "commission": -2.40, "close_reason": "SL", "account_mode": "real",
    }


MESSAGE_CATALOG: list[tuple[str, str, "callable"]] = [
    ("msg_worker_offline",
     "Equity monitor missed N consecutive 30s queries to a worker",
     lambda: msg_worker_offline("prop", 90)),
    ("msg_worker_back_online",
     "Worker resumed responding after being marked offline",
     lambda: msg_worker_back_online("prop")),
    ("msg_algo_trading_disabled",
     "MT5 reports trade_allowed=False — autotrading button is off",
     lambda: msg_algo_trading_disabled("personal")),
    ("msg_algo_trading_restored",
     "MT5 trade_allowed flipped back to True on a worker that was disabled",
     lambda: msg_algo_trading_restored("personal")),
    ("msg_new_session_auto_resumed",
     "Prop-firm day rolled past 11:00 SGT — daily K1/K3 halt auto-cleared",
     lambda: msg_new_session_auto_resumed()),
    ("msg_curfew_close",
     "SGT curfew transition (start of curfew window) — positions force-closed",
     lambda: msg_curfew_close(_demo_pos_str(), "12:00 SGT")),
    ("msg_mismatch_resolved",
     "Position mismatch persisted ≥120 s — bot force-closed the orphan leg",
     lambda: msg_mismatch_resolved(
         ticker="EURUSD", mismatch_type="prop_only",
         prop_dir=0, pers_dir=None,
         post_prop_open=False, post_pers_open=False, post_query_ok=True,
     )),
    ("msg_news_window_cleared",
     "Pre-close monitor finds expired suppression windows",
     lambda: msg_news_window_cleared([("EURUSD", "21:30 SGT")])),
    ("msg_news_pre_close",
     "High-impact ForexFactory event entered ban zone (≤30 min away)",
     lambda: msg_news_pre_close(
         currency="USD",
         event_title="CPI m/m",
         event_time_sgt="20:30 SGT", mins_to_event=12,
         affected_pers=[{"symbol": "EURUSD", "type": 1, "volume": 0.10, "profit": 8.20}],
         affected_prop=[{"symbol": "EURUSD", "type": 0, "volume": 0.50, "profit": -41.00}],
         suppression_end_sgt="21:00 SGT",
         pers_currency="SGD",
     )),
    ("msg_phase1_stage_reached",
     "Phase 1: prop equity ≥ active stage value (profitable day locked)",
     lambda: msg_phase1_stage_reached(5240.0, 5200.0, "12:00 SGT")),
    ("msg_kill1_phase1",
     "Phase 1 K1: daily loss limit hit (soft, auto-resumes next session)",
     lambda: msg_kill1_phase1(4850.0, 4900.0, 5000.0, "12:00 SGT")),
    ("msg_kill2_phase1",
     "Phase 1 K2: overall drawdown limit hit (permanent)",
     lambda: msg_kill2_phase1(4500.0, 4500.0)),
    ("msg_kill4_phase1_passed",
     "Phase 1 K4: prop equity reached the funded line (via strategy decision)",
     lambda: msg_kill4_phase1_passed(5500.0)),
    ("msg_kill2_phase2plus",
     "Phase 2+ K2: overall drawdown floor hit (permanent halt)",
     lambda: msg_kill2_phase2plus(95000.0, 95000.0, 5.0, 100000.0, _demo_pos_str())),
    ("msg_kill1_phase2plus",
     "Phase 2+ K1: daily loss limit hit (soft, auto-resumes next session)",
     lambda: msg_kill1_phase2plus(98500.0, 98500.0, 100000.0, 1500.0, 1.5,
                                  _demo_pos_str(), 95000.0, "12:00 SGT")),
    ("msg_kill3_daily_profit_cap",
     "K3: daily profit cap hit (protects Phase 2 consistency rule)",
     lambda: msg_kill3_daily_profit_cap(102000.0, 102000.0, 100000.0, 2000.0,
                                        _demo_pos_str(), "12:00 SGT")),
    ("msg_kill4_phase1_via_target",
     "Phase 1 K4 via profit-target branch (same outcome, includes snapshot)",
     lambda: msg_kill4_phase1_via_target(5500.0, 10.0, _demo_pos_str())),
    ("msg_kill4_phase2plus",
     "Phase 2+ K4: profit target reached (permanent halt)",
     lambda: msg_kill4_phase2plus(2, 110000.0, 10.0, _demo_pos_str())),
    ("msg_kill5_consistency",
     "Phase 2 K5: consistency rule met → claim profit share, start new cycle",
     lambda: msg_kill5_consistency()),
    ("msg_trade_opened",
     "_verify_and_notify saw status=FILLED on both legs",
     lambda: msg_trade_opened(
         ticker="EURUSD", phase=2,
         phase_context_extra="Baseline: $100,000.00",
         pers_arrow="↑ LONG", pers_lots=0.10,
         pers_entry_fmt="1.08234", pers_sl_fmt="1.07900", pers_tp_fmt="1.08600",
         pers_dollar_risk=33.40, pers_reward=36.60, pers_rr=1.10,
         pers_ticket=987654321, pers_currency="SGD", pers_usd_to_acct_rate=1.34,
         prop_arrow="↓ SHORT", prop_lots=0.50,
         prop_entry_fmt="1.08234", prop_sl_fmt="1.08600", prop_tp_fmt="1.07900",
         prop_dollar_risk=167.00, prop_reward=183.00, prop_rr=1.10,
         prop_ticket=987654322,
     )),
    ("msg_position_closed",
     "_detect_closes() flushed a pending close (both sides done OR 120s lapsed)",
     lambda: msg_position_closed(
         symbol="EURUSD",
         pers_pos_data=_demo_close_pos(), prop_pos_data={**_demo_close_pos(), "type": 1, "profit": 10.0},
         pers_deal=_demo_close_deal_found(),
         prop_deal={**_demo_close_deal_found(), "net_pnl": 10.0, "close_reason": "TP", "close_price": 1.07900},
         curr_pers=[], curr_prop=[],
         pers_currency="SGD", pers_eq_str="SGD 6,716.68", prop_eq_str="$99,987.55",
         is_news_close=False, account_mode="real",
     )),
    ("msg_signal_not_placed_terminal",
     "First status query returned REJECTED/ERROR/UNSUPPORTED on a leg",
     lambda: msg_signal_not_placed_terminal(
         ticker="EURUSD",
         pers_status={"status": "REJECTED", "broker_comment": "No money"},
         prop_status={"status": "FILLED"},
         pers_arrow="↑ LONG",
         entry_fmt="1.08234", pers_sl_fmt="1.07900", pers_tp_fmt="1.08600",
     )),
    ("msg_signal_not_placed_preflight",
     "Pre-flight order_check rejected one leg — nothing was sent (Issue 2)",
     lambda: msg_signal_not_placed_preflight(
         ticker="EURUSD",
         pers_chk={"verdict": "reject", "retcode": 10019,
                   "margin": 250.0, "margin_free": -10.0},
         prop_chk={"verdict": "ok"},
         pers_arrow="↑ LONG",
         entry_fmt="1.08234", pers_sl_fmt="1.07900", pers_tp_fmt="1.08600",
         pers_currency="SGD",
     )),
    ("msg_order_not_filled",
     "At least one leg didn't reach FILLED in the poll horizon (or LIMIT resting)",
     lambda: msg_order_not_filled(
         ticker="EURUSD", resting=False,
         pers_final={"status": "TIMEOUT"},
         prop_final={"status": "FILLED", "actual_fill_price": 1.08234,
                     "mt5_order_ticket": 987654321},
         entry=1.08234,
         pers_arrow="↑ LONG",
         pers_sl_fmt="1.07900", pers_tp_fmt="1.08600",
         pers_lots=0.10, prop_lots=0.50,
     )),
    ("msg_signal_blocked_p_halt",
     "Signal arrived while system permanently halted (K2/K4/K5)",
     lambda: msg_signal_blocked_p_halt("EURUSD", "LONG")),
    ("msg_signal_skipped_halted",
     "Signal arrived while system halted (K1/K3 daily halt or /stop)",
     lambda: msg_signal_skipped_halted("EURUSD", "LONG")),
    ("msg_signal_suppressed",
     "Signal arrived on a pair under news ban or manual /closepair block",
     lambda: msg_signal_suppressed("EURUSD", "LONG", "news suppression window")),
    ("msg_signal_skipped_max_pos",
     "Signal arrived but prop account already at max_open_positions cap",
     lambda: msg_signal_skipped_max_pos("EURUSD", "LONG", 2, 2)),
    ("msg_signal_blocked_algo_disabled",
     "Signal-time worker reply reports trade_allowed=False",
     lambda: msg_signal_blocked_algo_disabled("EURUSD", "prop")),
    ("msg_signal_blocked_generic",
     "Config-side error makes signal unsizable (Phase 1 not configured, etc.)",
     lambda: msg_signal_blocked_generic(
         "EURUSD",
         main="Phase 1 not configured.",
         command="/phase1",
         tail="to set reward:risk first.",
     )),
    ("msg_geometry_reject",
     "phase1_strategy or phase2_strategy returned a 'reject' key",
     lambda: msg_geometry_reject("EURUSD", 2, "lots below broker minimum", "LONG")),
    ("msg_internal_error",
     "_verify_and_notify task crashed before producing a Trade Opened/Not Filled alert",
     lambda: msg_internal_error("EURUSD", "TimeoutError: REQ socket")),
    ("msg_contract_query_failed",
     "Layer 3 /equity query threw at signal-processing time (plain text)",
     lambda: msg_contract_query_failed("prop", "Timeout after 5s")),
    ("msg_baseline_missing",
     "Signal arrived before /phase1 or /phase2 set baseline_equity (plain text)",
     lambda: msg_baseline_missing()),
    ("msg_invalid_contract_data",
     "Prop worker returned tick_size ≤ 0 or tick_value ≤ 0 (plain text)",
     lambda: msg_invalid_contract_data("EURUSD", 0.0, 1.0)),
    ("msg_tp_distance_zero",
     "Signal's TP equals entry — sizing would divide by zero (plain text)",
     lambda: msg_tp_distance_zero("EURUSD", 1.08234, 1.08234)),
    ("msg_dispatch_failed",
     "ZMQ push of ticket to a Layer 3 worker raised (plain text)",
     lambda: msg_dispatch_failed("prop", "Address in use")),
]


# Page size: keep each /messages run under Telegram's ~20-msg bot burst limit.
# Two pages cover the whole catalog: /messages → 1-19, /messages2 → 20-end.
_MESSAGES_PAGE_SIZE = 19


async def _send_messages_page(update: Update, page: int) -> None:
    """Render one page of MESSAGE_CATALOG as separate Telegram messages."""
    total = len(MESSAGE_CATALOG)
    total_pages = (total + _MESSAGES_PAGE_SIZE - 1) // _MESSAGES_PAGE_SIZE
    if page < 1 or page > total_pages:
        await update.message.reply_text(
            f"⚠️ Page {page} out of range. Valid pages: 1–{total_pages}.",
            parse_mode="HTML",
        )
        return

    start_idx = (page - 1) * _MESSAGES_PAGE_SIZE  # 0-based
    end_idx   = min(start_idx + _MESSAGES_PAGE_SIZE, total)

    await update.message.reply_text(
        f"<b>Telegram Message Catalog — Page {page}/{total_pages}</b>\n\n"
        f"Showing templates {start_idx + 1}–{end_idx} of {total}.",
        parse_mode="HTML",
    )

    for idx in range(start_idx, end_idx):
        name, trigger, render = MESSAGE_CATALOG[idx]
        human_idx = idx + 1
        try:
            preview = render()
        except Exception as exc:
            preview = f"(preview failed to render: {exc!r})"
        header = (
            f"<b>[{human_idx}/{total}] {name}</b>\n"
            f"<i>Triggered when:</i> {trigger}\n"
            f"────────────\n"
        )
        body = preview
        max_body = 4096 - len(header) - 40
        if len(body) > max_body:
            body = body[:max_body] + "\n…\n[preview truncated]"
        try:
            await update.message.reply_text(header + body, parse_mode="HTML")
        except Exception as exc:
            logger.warning("messages send failed at idx=%d: %r", human_idx, exc)
            try:
                await update.message.reply_text(
                    f"{header}(send failed: {exc!r}) — raw preview:\n{body[:1500]}",
                    parse_mode=None,
                )
            except Exception as exc2:
                logger.error("messages fallback also failed at idx=%d: %r", human_idx, exc2)
        await asyncio.sleep(1.0)  # generous spacing to stay clear of bot flood limits

    if page < total_pages:
        await update.message.reply_text(
            f"✅ End of page {page}/{total_pages}. "
            f"Send /messages{page + 1} for the next page.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"✅ End of catalog ({total} templates across {total_pages} pages). "
            f"Reply with the numbers to redesign.",
            parse_mode="HTML",
        )


async def _cmd_messages(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Catalog every Telegram message + its trigger condition (page 1).

    Sends one Telegram message per entry in MESSAGE_CATALOG. Pages of
    _MESSAGES_PAGE_SIZE entries to stay under Telegram's bot flood limit.
    `/messages` or `/messages 1` → page 1; `/messages 2` → page 2; etc.
    """
    if not _auth(update):
        return
    page = 1
    args = (ctx.args or []) if ctx else []
    if args:
        try:
            page = int(args[0])
        except ValueError:
            page = 1
    await _send_messages_page(update, page)


async def _cmd_messages2(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Catalog page 2 (continuation of /messages). See _cmd_messages."""
    if not _auth(update):
        return
    await _send_messages_page(update, 2)


# ── Bot startup ───────────────────────────────────────────────────────────

def _run_bot() -> None:
    wizard = ConversationHandler(
        entry_points=[CommandHandler("changepropfirm", _cmd_changepropfirm)],
        states={
            PF_NAME:           [CommandHandler("back", _wiz_back_step1),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_name)],
            PF_PROFIT_TARGET:  [CommandHandler("back", _wiz_back_step2),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_profit_target)],
            PF_MAX_DD_OVERALL: [CommandHandler("back", _wiz_back_step3),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_max_dd_overall)],
            PF_MAX_DD_DAILY:   [CommandHandler("back", _wiz_back_step4),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_max_dd_daily)],
            PF_DD_TYPE:        [CommandHandler("back", _wiz_back_step5),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_dd_type)],
            PF_RAW_SPREAD:     [CommandHandler("back", _wiz_back_step6),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_raw_spread)],
            PF_PROFIT_SHARE:   [CommandHandler("back", _wiz_back_step7),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_profit_share)],
            PF_MIN_DAYS:       [CommandHandler("back", _wiz_back_step8),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_min_days)],
            PF_CONSISTENCY:    [CommandHandler("back", _wiz_back_step9),  MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_consistency)],
            PF_INITIAL_BALANCE:[CommandHandler("back", _wiz_back_step10), MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_initial_balance)],
            PF_CONFIRM:        [CommandHandler("back", _wiz_back_confirm), MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _wiz_cancel)],
        per_chat=True,
        allow_reentry=True,
    )

    p2_wizard = ConversationHandler(
        entry_points=[CommandHandler("phase2", _cmd_phase2)],
        states={
            P2_SAME_OR_DIFF:    [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_same_or_diff)],
            P2_WHICH_FIELDS:    [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_which_fields)],
            P2_COLLECTING:      [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_collect_field)],
            P2_INITIAL_BALANCE: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_initial_balance)],
            P2_PERS_BALANCE:    [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_pers_balance)],
            P2_CONFIRM:         [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _p2_cancel)],
        per_chat=True,
        allow_reentry=True,
    )

    emergency_wizard = ConversationHandler(
        entry_points=[CommandHandler("emergency", _cmd_emergency)],
        states={
            EMERGENCY_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _emergency_execute)],
        },
        fallbacks=[CommandHandler("cancel", _emergency_abort)],
        per_chat=True,
        allow_reentry=True,
    )

    closepair_wizard = ConversationHandler(
        entry_points=[CommandHandler("closepair", _cmd_closepair)],
        states={
            CLOSEPAIR_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _closepair_execute)],
        },
        fallbacks=[CommandHandler("cancel", _closepair_abort)],
        per_chat=True,
        allow_reentry=True,
    )

    setwindow_wizard = ConversationHandler(
        entry_points=[CommandHandler("setwindow", _cmd_setwindow)],
        states={
            SETWINDOW_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _setwindow_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _setwindow_abort)],
        per_chat=True,
        allow_reentry=True,
    )

    phase1_wizard = ConversationHandler(
        entry_points=[CommandHandler("phase1", _cmd_phase1)],
        states={
            P1_INPUT:   [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p1_input)],
            P1_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p1_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _p1_cancel)],
        per_chat=True,
        allow_reentry=True,
    )

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(wizard)
    tg_app.add_handler(p2_wizard)
    tg_app.add_handler(emergency_wizard)
    tg_app.add_handler(closepair_wizard)
    tg_app.add_handler(setwindow_wizard)
    tg_app.add_handler(phase1_wizard)
    tg_app.add_handler(CommandHandler("stop",          _cmd_stop))
    tg_app.add_handler(CommandHandler("resume",        _cmd_resume))
    tg_app.add_handler(CommandHandler("rearm",         _cmd_rearm))
    tg_app.add_handler(CommandHandler("status",        _cmd_status))
    tg_app.add_handler(CommandHandler("propfirm",      _cmd_propfirm))
    tg_app.add_handler(CommandHandler("equity",         _cmd_equity))
    tg_app.add_handler(CommandHandler("setbaseline",    _cmd_setbaseline))
    tg_app.add_handler(CommandHandler("setdayroll",     _cmd_setdayroll))
    tg_app.add_handler(CommandHandler("setdeposit",     _cmd_setdeposit))
    tg_app.add_handler(CommandHandler("changepropfirm", _cmd_changepropfirm))
    tg_app.add_handler(CommandHandler("positions",     _cmd_positions))
    tg_app.add_handler(CommandHandler("pnl",           _cmd_pnl))
    tg_app.add_handler(CommandHandler("health",        _cmd_health))
    tg_app.add_handler(CommandHandler("checksymbols",  _cmd_checksymbols))
    tg_app.add_handler(CommandHandler("news",          _cmd_news))
    tg_app.add_handler(CommandHandler("blackboard",    _cmd_blackboard))
    tg_app.add_handler(CommandHandler("resumepair",    _cmd_resumepair))
    tg_app.add_handler(CommandHandler("setmaxpos",     _cmd_setmaxpos))
    tg_app.add_handler(CommandHandler("maxpos",        _cmd_maxpos))
    tg_app.add_handler(CommandHandler("consistency",    _cmd_consistency))
    update_wizard = ConversationHandler(
        entry_points=[CommandHandler("update", _cmd_update)],
        states={
            UPDATE_LAYER3_CHOOSE: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _update_layer3_choose)],
        },
        fallbacks=[CommandHandler("cancel", _update_cancel)],
        per_chat=True,
        allow_reentry=True,
    )
    tg_app.add_handler(update_wizard)
    tg_app.add_handler(CommandHandler("checkaccount",  _cmd_checkaccount))
    tg_app.add_handler(CommandHandler("help",          _cmd_help))
    tg_app.add_handler(CommandHandler("setwindow",     _cmd_setwindow))
    tg_app.add_handler(CommandHandler("messages",      _cmd_messages))
    tg_app.add_handler(CommandHandler("messages2",     _cmd_messages2))
    tg_app.add_handler(CommandHandler("cancel",        _cmd_cancel_noop))  # fallback when no wizard active

    async def _poll():
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=["message"])
        logger.info("Telegram bot polling (chat_id=%d)", CHAT_ID)
        await asyncio.Event().wait()  # block forever; thread is daemon so exits with process

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_poll())
