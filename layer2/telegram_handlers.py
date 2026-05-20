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
    _apply_buffers, _pnl_bar, _fmt_price,
    _trading_window, _window_lock, _save_trading_window, _window_minutes,
    _phase1_init, _phase1_load,
)
from layer2.zmq_helpers import (
    _query_equity, _query_positions, _snapshot_positions_str,
    _dispatch_force_close, _dispatch_close_ticker, _dispatch_news_suppress,
    _dispatch_news_clear, _close_ticker_on_worker,
    _telegram_alert, _alert_sync,
    _lock_baseline_from_live, _dispatch_parameters,
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
            "⚠️ <b>Dynamic Drawdown Flagged</b>\n\n"
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
            "⚠️ <b>Non-Raw Spread Flagged</b>\n\n"
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

    summary = (
        f"📊 <b>Review Account Setup</b>\n\n"
        f"<b>Prop Rules</b>\n"
        f"Profit target: {_wizard_data['profit_target_pct']:.1f}%\n"
        f"Overall DD: {_wizard_data['max_drawdown_overall_pct']:.1f}%\n"
        f"Daily DD: {daily_dd_raw:.1f}% → {daily_dd_eff:.1f}%\n"
        f"Consistency: {cons_raw:.1f}% → {cons_eff:.1f}%\n\n"
        f"<b>Baselines</b>\n"
        f"Prop: ${prop_b:,.2f}\n"
        f"Personal: ${v:,.2f}\n\n"
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
            "🟡 <b>Cancelled</b>\n\nNo changes were saved.",
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

    dd_daily   = eff["max_drawdown_daily_pct"]
    dd_overall = eff["max_drawdown_overall_pct"]
    cap        = eff["daily_profit_cap_pct"]
    target_pct = _wizard_data["profit_target_pct"]
    daily_dd_amt = round(baseline * dd_daily   / 100.0, 2) if baseline > 0 else 0.0
    cap_amt      = round(baseline * cap        / 100.0, 2) if baseline > 0 else 0.0
    overall_fl   = round(baseline * (1 - dd_overall / 100.0), 2) if baseline > 0 else 0.0
    target_lvl   = round(baseline * (1.0 + target_pct / 100.0), 2) if baseline > 0 else 0.0

    _wizard_data.clear()
    await update.message.reply_text(
        f"✅ <b>Account Setup Saved</b>\n\n"
        f"<b>Baselines</b>\n"
        f"Prop: ${baseline:,.2f}\n"
        f"Personal: ${pers_baseline:,.2f}\n\n"
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
        "🟡 <b>Cancelled</b>\n\nNo changes were saved.",
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
                f"⚠️ <b>Baseline Missing</b>\n\nCould not set baseline: <code>{err}</code>\n\n"
                f"Run /changepropfirm first, then /phase1 again.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        baseline = balance

    verr = phase1_strategy.validate_phase1_inputs(
        first_reward, fixed_risk, baseline, target_pct, min_days)
    if verr:
        await update.message.reply_text(
            f"⚠️ <b>Cannot Configure Phase 1</b>\n\n{verr}",
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
    if fixed_risk >= overall_amt - (baseline - stages[0]):
        warn = ""  # placeholder; no daily-DD figure available pre-session
    daily_room = baseline * pf.get("max_drawdown_daily_pct", 0.0) / 100.0
    if daily_room > 0 and fixed_risk >= daily_room:
        warn = (f"\n\n⚠️ Risk ${fixed_risk:,.0f} ≥ daily-DD room ${daily_room:,.0f} "
                f"— only one losing trade fits per day.")

    await update.message.reply_text(
        f"✅ <b>Phase 1 Ready</b>\n\n"
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
    _wizard_data.clear()

    stage_str = "  →  ".join(f"${s:,.0f}" for s in d["stages"])
    await update.message.reply_text(
        f"🟢 <b>Phase 1 Active</b>\n\n"
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
            "⚠️ <b>Phase 1 Config Missing</b>\n\n"
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
        f"🟢 <b>Phase 2 Setup</b>\n\n"
        f"<b>Phase 1 Settings</b>\n<pre>{block}</pre>\n\n"
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
            f"<b>Current Settings</b>\n<pre>{block}</pre>\n\n"
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
        f"📊 <b>Phase 2 Review</b>\n\n"
        f"<pre>{block}</pre>\n\n"
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
        await update.message.reply_text("🟡 <b>Cancelled</b>\n\nNo changes were saved.", parse_mode="HTML")
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

    floor_amt    = round(baseline * (1.0 - eff["max_drawdown_overall_pct"] / 100.0), 2) if baseline > 0 else 0.0
    daily_dd_amt = round(baseline * eff["max_drawdown_daily_pct"]  / 100.0, 2) if baseline > 0 else 0.0
    cap_amt      = round(baseline * eff["daily_profit_cap_pct"]    / 100.0, 2) if baseline > 0 else 0.0
    target_lvl   = round(baseline * (1.0 + new["profit_target_pct"] / 100.0), 2) if baseline > 0 else 0.0

    _p2_wizard_data.clear()
    await update.message.reply_text(
        f"🟢 <b>Phase 2 Active</b>\n\n"
        f"<b>Risk Mode</b>\n"
        f"Personal multiplier: ×{PHASE_MULT[2]:.2f}\n\n"
        f"<b>Baselines</b>\n"
        f"Prop: ${baseline:,.2f}\n"
        f"Personal: ${pers_baseline:,.2f}\n\n"
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
        "🟡 <b>Cancelled</b>\n\nNo changes were saved.",
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

    def _pos_line(positions: list[dict]) -> str:
        if not positions:
            return "No open positions"
        return ", ".join(
            f"{p['symbol']} {'↑ LONG' if p['type'] == 0 else '↓ SHORT'} {p['volume']:.2f} lots"
            for p in positions
        )

    lines: list[str] = [
        "🟡 <b>Signal Processing Halted</b>\n",
        "New signals will be ignored.",
        "Existing open trades remain active unless closed manually.\n",
        "<b>Open Positions</b>",
        f"Personal Signal: {_pos_line(pers_pos)}",
        f"Prop Hedge: {_pos_line(prop_pos)}\n",
        "<b>Next Step</b>",
        "/resume — re-enable signals",
        "/emergency — force-close all positions",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    logger.warning("Telegram: halted by user")


async def _cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        p_halt = _phase_state.get("permanently_halted", False)
    if p_halt:
        await update.message.reply_text(
            "🔴 <b>Resume Blocked</b>\n\n"
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

    def _pos_line(positions: list[dict]) -> str:
        if not positions:
            return "No open positions"
        return ", ".join(
            f"{p['symbol']} {'↑ LONG' if p['type'] == 0 else '↓ SHORT'} {p['volume']:.2f} lots"
            for p in positions
        )

    curfew_note = "\n\n<i>Trading window is currently closed. Signals resume when the window opens.</i>" if _is_sgt_curfew() else ""
    override_note = (
        "\n\n<i>Today's daily-loss / profit-cap kills are suppressed until the next session — manual override active.</i>"
        if had_daily_halt else ""
    )
    lines: list[str] = [
        f"🟢 <b>Signal Processing Resumed</b>",
        "",
        "New signals are now allowed.\n",
        "<b>Open Positions</b>",
        f"Personal Signal: {_pos_line(pers_pos)}",
        f"Prop Hedge: {_pos_line(prop_pos)}",
    ]
    if override_note:
        lines.append(override_note)
    if curfew_note:
        lines.append(curfew_note)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    logger.info("Telegram: resumed by user — soft_kill_override_day=%s", today_day)


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
        f"📊 <b>System Status</b>\n\n"
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
    await update.message.reply_text(
        f"📊 <b>Prop Account Rules</b>\n\n"
        f"<b>Targets</b>\n"
        f"Profit target: {pf.get('profit_target_pct', 0):.1f}%\n"
        f"Daily profit cap: {pf.get('daily_profit_cap_pct', 0):.1f}%\n\n"
        f"<b>Drawdown</b>\n"
        f"Overall DD: {pf.get('max_drawdown_overall_pct', 0):.1f}%\n"
        f"Daily DD: {pf.get('max_drawdown_daily_pct', 0):.1f}% (↓1pp)\n"
        f"Type: {'Static' if pf.get('drawdown_is_static') else 'Dynamic'}\n\n"
        f"<b>Other Rules</b>\n"
        f"Raw spread: {'Yes' if pf.get('raw_spread_account') else 'No'}\n"
        f"Profit sharing: {pf.get('profit_sharing_pct', 0):.1f}%\n"
        f"Min profit days: {pf.get('min_profit_days', 0)}\n"
        f"Consistency: {pf.get('consistency_threshold_pct', 0):.1f}% (↓1pp)\n\n"
        f"<b>Baselines</b>\n"
        f"Prop: ${prop_b:,.2f}\n"
        f"Personal: {f'${pers_b:,.2f}' if pers_b > 0 else 'Not set'}",
        parse_mode="HTML",
    )


async def _cmd_equity(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    with _pf_lock:
        pf = dict(_propfirm)
    pers_baseline = pf.get("pers_baseline_equity", 0.0)
    prop_baseline = pf.get("baseline_equity",       0.0)

    def _account_block(label: str, data: dict, baseline: float) -> str:
        bal     = data["balance"]
        eq      = data["equity"]
        floating = data.get("profit", eq - bal)
        lines = [
            f"<b>{label}</b>",
            f"Baseline: ${baseline:,.2f}" if baseline > 0 else "Baseline: Not set — run /changepropfirm",
            f"Balance: ${bal:,.2f}",
            f"Equity: ${eq:,.2f}",
            f"Floating: ${floating:+,.2f}",
        ]
        if baseline > 0:
            overall = eq - baseline
            overall_pct = overall / baseline * 100
            lines.append(f"Overall: ${overall:+,.2f} ({overall_pct:+.2f}%)")
        return "\n".join(lines)

    try:
        pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_block = _account_block("Personal Signal", pers, pers_baseline)
    except Exception as exc:
        pers_block = f"<b>Personal Signal</b>\nOffline — {exc}"

    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_block = _account_block("Prop Hedge", prop, prop_baseline)
    except Exception as exc:
        prop_block = f"<b>Prop Hedge</b>\nOffline — {exc}"

    await update.message.reply_text(
        f"📊 <b>Account Equity Snapshot</b>\n\n{pers_block}\n\n{prop_block}",
        parse_mode="HTML",
    )


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

    def _fmt_positions(positions: list[dict], err: str | None) -> str:
        if err:
            return f"Offline — {err}"
        if not positions:
            return "No open positions"
        return "\n".join(
            f"  {p['symbol']} {'↑ LONG' if p['type'] == 0 else '↓ SHORT'} {p['volume']:.2f} lots  P&amp;L: ${p['profit']:+,.2f}"
            for p in positions
        )

    lines = [
        "🔴 <b>Emergency Halt — Confirm Action</b>\n",
        "This will:\n• Force-close all open positions\n• Halt signal processing\n",
        "<b>Open Positions</b>",
        "",
        "<b>Personal Signal</b>",
        _fmt_positions(pers_pos, pers_err),
        "",
        "<b>Prop Hedge</b>",
        _fmt_positions(prop_pos, prop_err),
        "",
        "Reply <code>CONFIRM</code> to proceed.",
        "Send /cancel to abort.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
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
            f"Personal: ${pers_eq['equity']:,.2f}\n"
            f"Prop: ${prop_eq['equity']:,.2f}"
        )
    except Exception:
        eq_lines = "Could not query equity"
    await update.message.reply_text(
        f"🔴 <b>Emergency Halt Executed</b>\n\n"
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
        "🟡 <b>Emergency Cancelled</b>\n\nNo positions were closed.",
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

    def _fmt_block(label: str, positions: list[dict] | None, err: str | None) -> str:
        out = [f"<b>{label}</b>"]
        if err:
            out.append(f"Offline — {err}")
        elif not positions:
            out.append("No open positions")
        else:
            for p in positions:
                direction = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                out.append(
                    f"{p['symbol']} {direction} · {p['volume']:.2f} lots\n"
                    f"Entry {_fmt_price(p['symbol'], p['price_open'])} | SL {_fmt_price(p['symbol'], p['sl'])} | TP {_fmt_price(p['symbol'], p['tp'])}\n"
                    f"P&amp;L: ${p['profit']:+,.2f}"
                )
        return "\n".join(out)

    pers_block = _fmt_block("Personal Signal", pers_pos, pers_err)
    prop_block = _fmt_block("Prop Hedge", prop_pos, prop_err)

    await update.message.reply_text(
        f"📊 <b>Open Positions</b>\n\n{pers_block}\n\n{prop_block}",
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
        f"📊 <b>P&amp;L Dashboard — Prop Hedge</b>\n",
        f"<b>Account</b>",
        f"Baseline:      ${baseline:,.2f}",
        f"Day-start:     ${day_start:,.2f}",
        f"Current equity: ${equity:,.2f}",
    ]
    if daily_loss_amt > 0:
        lines += [
            f"\n<b>K1/K2 — Loss Protection</b>",
            f"Daily limit: ${daily_loss_amt:,.2f} ({daily_dd:.1f}% of day-start)  |  {k1_status}",
            f"Daily floor: <b>${daily_floor:,.2f}</b>  (resets each session)",
            f"Overall DD floor: ${overall_floor:,.2f}  (static from baseline)",
            f"<code>{_pnl_bar(k1_bar_pct)}</code>  {k1_bar_pct:.1f}% of daily limit used",
            f"<code>{_pnl_bar(k2_bar_pct)}</code>  {k2_bar_pct:.1f}% of overall DD consumed",
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
            f"<code>{_pnl_bar(k3_bar_pct)}</code>  {k3_bar_pct:.1f}% of daily cap used",
        ]
    if target_amt > 0:
        lines += [
            f"\n<b>K4 — Overall Target</b>",
            f"Target: {target_pct:.1f}% of baseline  =  ${target_amt:,.2f}",
            f"Target level: <b>${k4_target:,.2f}</b>",
            f"Progress: ${max(0.0, overall_pnl):,.2f} / ${target_amt:,.2f}",
            f"<code>{_pnl_bar(k4_bar_pct)}</code>  {k4_bar_pct:.1f}%",
        ]

    lines.append(
        "\n<i>Bars — K1: daily loss used (% of day-start, resets each session) · "
        "K2: total DD consumed from baseline · "
        "K3: daily profit cap used · "
        "K4: progress toward profit target</i>"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
        f"📊 <b>System Health</b>\n\n"
        f"Layer 1 (VPS #1): {l1}\n"
        f"Layer 2 (VPS #1): 🟢 Alive\n"
        f"Personal Signal (VPS #2): {pers_h}\n"
        f"Prop Hedge (VPS #3): {prop_h}",
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
            "📰 <b>Upcoming High-Impact News</b>\n\n"
            "🟢 No high-impact events in the next 4 hours for covered pairs.",
            parse_mode="HTML",
        )
        return

    lines = ["📰 <b>Upcoming High-Impact News</b> <i>Next 4 hours · Covered pairs only</i>\n"]
    for t, ccy, title, pairs in relevant:
        sgt_str   = (t + sgt_off).strftime("%H:%M SGT")
        pairs_str = ", ".join(pairs) if pairs else "—"
        lines.append(f"🟠 {sgt_str} — {ccy}: {title}\nAffects: {pairs_str}")

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def _cmd_blackboard(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    now     = datetime.now(timezone.utc)
    sgt_off = timedelta(hours=8)
    lines   = ["📊 <b>Suppression Blackboard</b>\n"]

    with _news_suppressed_lock:
        news_active = {t: e for t, e in _news_suppressed_pairs.items() if e > now}
    with _manual_suppress_lock:
        manual_active = set(_manual_suppressed_pairs)

    all_pairs = set(news_active) | manual_active
    if not all_pairs:
        await update.message.reply_text(
            "📊 <b>Suppression Blackboard</b>\n\n"
            "🟢 No active suppressions.\n"
            "All covered pairs are clear for new signals.",
            parse_mode="HTML",
        )
        return

    blocks: list[str] = ["📊 <b>Suppression Blackboard</b>"]
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

    await update.message.reply_text("\n\n".join(blocks), parse_mode="HTML")


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

    def _fmt_pair_positions(positions: list[dict], symbols: tuple) -> str:
        pair_pos = [p for p in positions if p["symbol"] in symbols]
        if not pair_pos:
            return "No open positions"
        return "\n".join(
            f"  {p['symbol']} {'↑ LONG' if p['type'] == 0 else '↓ SHORT'} {p['volume']:.2f} lots  P&amp;L: ${p['profit']:+,.2f}"
            for p in pair_pos
        )

    syms = (ticker, broker_symbol)
    try:
        pers_pos     = await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS)
        pers_pos_str = _fmt_pair_positions(pers_pos, syms)
    except Exception as exc:
        pers_pos_str = f"Offline — {exc}"
    try:
        prop_pos     = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
        prop_pos_str = _fmt_pair_positions(prop_pos, syms)
    except Exception as exc:
        prop_pos_str = f"Offline — {exc}"

    lines = [
        f"🟡 <b>Close Pair — {ticker}</b>\n",
        "This will:\n• Close all positions\n• Block new signals\n",
        "<b>Open Positions</b>",
        "",
        "<b>Personal Signal</b>",
        pers_pos_str,
        "",
        "<b>Prop Hedge</b>",
        prop_pos_str,
        "",
        "Reply <code>CONFIRM</code> to proceed.",
        "Send /cancel to abort.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
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
            f"Personal: ${pers_eq['equity']:,.2f}\n"
            f"Prop: ${prop_eq['equity']:,.2f}"
        )
    except Exception:
        eq_lines = "Could not query equity"
    await update.message.reply_text(
        f"✅ <b>Pair Closed and Blocked — {ticker}</b>\n\n"
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
        f"🟡 <b>Close Pair Cancelled — {ticker}</b>\n\nNo positions were closed.",
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
        f"🟢 <b>Pair Resumed — {ticker}</b>\n\nNew {ticker} signals are now allowed.",
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
        f"📊 <b>Max Position Limit Updated</b>\n\n"
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
        f"📊 <b>Position Limit</b>\n\n"
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
            "📊 <b>Consistency Tracker</b>\n\n"
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
            f"🟢 <b>Consistency Rule Met</b>\n\n"
            f"<pre>{table_str}</pre>\n\n"
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
        f"📊 <b>Consistency Tracker</b>\n\n"
        f"Threshold: &lt; {threshold:.1f}%\n\n"
        f"<pre>{table_str}</pre>\n\n"
        f"<b>Status</b>\n{status_line}",
        parse_mode="HTML",
    )


async def _cmd_setwindow(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    args = (update.message.text or "").split()[1:]
    if len(args) != 2:
        await update.message.reply_text(
            "🕒 <b>Trading Window Usage</b>\n\n"
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
        f"🕒 <b>Update Trading Window</b>\n\n"
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
            f"🕒 <b>Trading Window Updated</b>\n\n"
            f"Applied: Today, effective immediately\n"
            f"Window: <b>{start}–{end} SGT</b>",
            parse_mode="HTML",
        )
    elif choice in ("2", "tomorrow"):
        with _window_lock:
            _trading_window["next_window"] = new_window
            _save_trading_window()
        await update.message.reply_text(
            f"🕒 <b>Trading Window Scheduled</b>\n\n"
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
        "🟡 <b>Cancelled</b>\n\nNo changes were applied.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_cancel_noop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    await update.message.reply_text("No active wizard to cancel.", parse_mode="HTML")


# ── /changeaccount wizard ─────────────────────────────────────────────────

def _changeaccount_text_personal() -> str:
    return (
        "🔧 <b>Change Personal Signal MT5 Account</b>\n\n"
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
        "🔧 <b>Change Prop Hedge MT5 Account</b>\n\n"
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
        f"<b>Account Check</b>\n\n{pers_block}\n\n{prop_block}",
        parse_mode="HTML",
    )


# ── /update ───────────────────────────────────────────────────────────────

def _update_menu_text() -> str:
    return (
        "🛠️ <b>Update Guide</b>\n\n"
        "Choose what you want to update:\n\n"
        "/update local — Push local code to GitHub\n"
        "/update layer2 — Deploy latest code to VPS #1\n"
        "/update layer3 — Update a Layer 3 worker\n"
        "/update account — MT5 account change checklist"
    )


def _update_local_text() -> str:
    return (
        "🛠️ <b>Update Local Code → GitHub</b>\n\n"
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
        "🛠️ <b>Deploy Layer 2 — VPS #1</b>\n\n"
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
        "🛠️ <b>Update Personal Worker</b>\n\n"
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
        "🛠️ <b>Update Prop Worker</b>\n\n"
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
            "🛠️ <b>Update Layer 3 Worker</b>\n\n"
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
    await update.message.reply_text("Cancelled.", parse_mode="HTML")
    return ConversationHandler.END


async def _cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    await update.message.reply_text(
        "<b>HedgeHog Command Menu</b>\n\n"

        "<b>Emergency</b>\n"
        "/emergency — Force-close all positions and halt\n\n"

        "<b>Trading Control</b>\n"
        "/resume — Resume signal processing\n"
        "/stop — Halt new signals\n"
        "/phase1 — Start Phase 1\n"
        "/phase2 — Start Phase 2\n\n"

        "<b>Positions &amp; Risk</b>\n"
        "/positions — Show open positions\n"
        "/equity — Account equity snapshot\n"
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
        "/update — Maintenance and deployment guide",
        parse_mode="HTML",
    )


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
    )

    emergency_wizard = ConversationHandler(
        entry_points=[CommandHandler("emergency", _cmd_emergency)],
        states={
            EMERGENCY_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _emergency_execute)],
        },
        fallbacks=[CommandHandler("cancel", _emergency_abort)],
        per_chat=True,
    )

    closepair_wizard = ConversationHandler(
        entry_points=[CommandHandler("closepair", _cmd_closepair)],
        states={
            CLOSEPAIR_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _closepair_execute)],
        },
        fallbacks=[CommandHandler("cancel", _closepair_abort)],
        per_chat=True,
    )

    setwindow_wizard = ConversationHandler(
        entry_points=[CommandHandler("setwindow", _cmd_setwindow)],
        states={
            SETWINDOW_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _setwindow_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _setwindow_abort)],
        per_chat=True,
    )

    phase1_wizard = ConversationHandler(
        entry_points=[CommandHandler("phase1", _cmd_phase1)],
        states={
            P1_INPUT:   [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p1_input)],
            P1_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p1_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _p1_cancel)],
        per_chat=True,
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
    tg_app.add_handler(CommandHandler("status",        _cmd_status))
    tg_app.add_handler(CommandHandler("propfirm",      _cmd_propfirm))
    tg_app.add_handler(CommandHandler("equity",         _cmd_equity))
    tg_app.add_handler(CommandHandler("changepropfirm", _cmd_changepropfirm))
    tg_app.add_handler(CommandHandler("positions",     _cmd_positions))
    tg_app.add_handler(CommandHandler("pnl",           _cmd_pnl))
    tg_app.add_handler(CommandHandler("health",        _cmd_health))
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
    )
    tg_app.add_handler(update_wizard)
    tg_app.add_handler(CommandHandler("checkaccount",  _cmd_checkaccount))
    tg_app.add_handler(CommandHandler("help",          _cmd_help))
    tg_app.add_handler(CommandHandler("setwindow",     _cmd_setwindow))
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
