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
    _apply_buffers, _pnl_bar,
    _trading_window, _window_lock, _save_trading_window, _window_minutes,
)
from layer2.zmq_helpers import (
    _query_equity, _query_positions, _snapshot_positions_str,
    _dispatch_force_close, _dispatch_close_ticker, _dispatch_news_suppress,
    _dispatch_news_clear, _close_ticker_on_worker,
    _telegram_alert, _alert_sync,
    _lock_baseline_from_live, _dispatch_parameters,
    ZMQ_REQ_PROP, ZMQ_REQ_PERS, ZMQ_PUSH_PROP, ZMQ_PUSH_PERS,
)

logger = logging.getLogger("layer2")

# ── Telegram wizard — /changepropfirm ────────────────────────────────────

(PF_NAME, PF_PROFIT_TARGET, PF_MAX_DD_OVERALL, PF_MAX_DD_DAILY,
 PF_DD_TYPE, PF_RAW_SPREAD, PF_PROFIT_SHARE, PF_MIN_DAYS,
 PF_CONSISTENCY, PF_INITIAL_BALANCE, PF_CONFIRM) = range(11)

(P2_SAME_OR_DIFF, P2_WHICH_FIELDS, P2_COLLECTING, P2_INITIAL_BALANCE, P2_CONFIRM) = range(10, 15)

EMERGENCY_CONFIRM  = 15
CLOSEPAIR_CONFIRM  = 16
SETWINDOW_CONFIRM  = 17

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
        "🏦 <b>Prop Firm Setup</b>\n\n"
        "<b>Step 1/10 — Firm Name</b>\n"
        "Enter the prop firm name:",
        parse_mode="HTML",
    )
    return PF_NAME


async def _wiz_name(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _wizard_data["propfirm_name"] = update.message.text.strip()
    await update.message.reply_text(
        "<b>Step 2/10 — Profit Target</b>\n\n"
        "Enter the firm’s profit target percentage.\n"
        "Example: <code>10</code>",
        parse_mode="HTML",
    )
    return PF_PROFIT_TARGET


async def _wiz_profit_target(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>10</code>", parse_mode="HTML")
        return PF_PROFIT_TARGET
    _wizard_data["profit_target_pct"] = v
    await update.message.reply_text(
        "<b>Step 3/10 — Overall Drawdown</b>\n\n"
        "Enter the firm’s raw overall drawdown limit.\n"
        "Example: <code>10</code>\n\n"
        "⚠️ No automatic buffer is applied — the value you enter is enforced exactly.\n"
        "Enter the firm’s stated limit as-is.",
        parse_mode="HTML",
    )
    return PF_MAX_DD_OVERALL


async def _wiz_max_dd_overall(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>10</code>", parse_mode="HTML")
        return PF_MAX_DD_OVERALL
    _wizard_data["max_drawdown_overall_pct"] = v
    await update.message.reply_text(
        "<b>Step 4/10 — Daily Drawdown</b>\n\n"
        "Enter the firm’s raw daily drawdown limit.\n"
        "Example: <code>3</code>\n\n"
        "⚠️ Enter the firm’s stated limit WITHOUT buffer.\n"
        "The system will subtract 1pp automatically.\n"
        "(e.g. firm says 3% → enter <code>3</code> → bot triggers at 2%)",
        parse_mode="HTML",
    )
    return PF_MAX_DD_DAILY


async def _wiz_max_dd_daily(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("⚠️ <b>Invalid Input</b>\n\nEnter a positive number.\nExample: <code>3</code>", parse_mode="HTML")
        return PF_MAX_DD_DAILY
    _wizard_data["max_drawdown_daily_pct"] = v
    await update.message.reply_text(
        "<b>Step 5/10 — Drawdown Type</b>\n\n"
        "Type one option:\n"
        "<code>static</code> or <code>dynamic</code>",
        parse_mode="HTML",
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
                "⚠️ <b>Dynamic Drawdown Accepted</b>\n\n"
                "This account is now flagged as dynamic drawdown.\n\n"
                "<b>Step 6/10 — Raw Spread Account</b>\n"
                "Type <code>yes</code> or <code>no</code>:",
                parse_mode="HTML",
            )
            return PF_RAW_SPREAD
        else:
            _wizard_data.pop("_dd_type_confirming")
            await update.message.reply_text(
                "⚠️ <b>Confirmation Not Received</b>\n\n"
                "Re-enter drawdown type:\n"
                "<code>static</code> or <code>dynamic</code>",
                parse_mode="HTML",
            )
            return PF_DD_TYPE

    v_lower = v.lower()
    if v_lower == "static":
        _wizard_data["drawdown_is_static"] = True
        await update.message.reply_text(
            "<b>Step 6/10 — Raw Spread Account</b>\n\n"
            "Type one option:\n"
            "<code>yes</code> or <code>no</code>",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD
    elif v_lower == "dynamic":
        _wizard_data["_dd_type_confirming"] = True
        await update.message.reply_text(
            "⚠️ <b>Dynamic Drawdown Flagged</b>\n\n"
            "This system is designed for static drawdown accounts.\n\n"
            "Reply <b>CONFIRM</b> to accept dynamic drawdown, or type <code>static</code> to correct.",
            parse_mode="HTML",
        )
        return PF_DD_TYPE
    else:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nType exactly one option:\n<code>static</code> or <code>dynamic</code>",
            parse_mode="HTML",
        )
        return PF_DD_TYPE


async def _wiz_raw_spread(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip()

    # Confirmation step — user previously entered "no" and was warned
    if _wizard_data.get("_raw_spread_confirming"):
        if v.upper() == "CONFIRM":
            _wizard_data["raw_spread_account"] = False
            _wizard_data.pop("_raw_spread_confirming")
            await update.message.reply_text(
                "⚠️ <b>Non-Raw Spread Accepted</b>\n\n"
                "This account is now flagged as non-raw spread.\n\n"
                "<b>Step 7/10 — Profit Sharing</b>\n"
                "Enter the profit sharing percentage. Example: <code>80</code>",
                parse_mode="HTML",
            )
            return PF_PROFIT_SHARE
        else:
            _wizard_data.pop("_raw_spread_confirming")
            await update.message.reply_text(
                "⚠️ <b>Confirmation Not Received</b>\n\n"
                "Re-enter one option:\n"
                "<code>yes</code> or <code>no</code>",
                parse_mode="HTML",
            )
            return PF_RAW_SPREAD

    v_lower = v.lower()
    if v_lower == "yes":
        _wizard_data["raw_spread_account"] = True
        await update.message.reply_text(
            "<b>Step 7/10 — Profit Sharing</b>\n\n"
            "Enter the profit sharing percentage.\n"
            "Example: <code>80</code>",
            parse_mode="HTML",
        )
        return PF_PROFIT_SHARE
    elif v_lower == "no":
        _wizard_data["_raw_spread_confirming"] = True
        await update.message.reply_text(
            "⚠️ <b>Non-Raw Spread Flagged</b>\n\n"
            "This system is designed for raw spread accounts.\n\n"
            "Reply <b>CONFIRM</b> to accept non-raw spread, or type <code>yes</code> to correct.",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD
    else:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nType exactly one option:\n<code>yes</code> or <code>no</code>",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD


async def _wiz_profit_share(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert 0 < v <= 100
    except Exception:
        await update.message.reply_text("⚠️ <b>Invalid Input</b>\n\nEnter a number between 1 and 100.", parse_mode="HTML")
        return PF_PROFIT_SHARE
    _wizard_data["profit_sharing_pct"] = v
    await update.message.reply_text(
        "<b>Step 8/10 — Minimum Profit Days</b>\n\n"
        "Enter the minimum trading days required.\n"
        "Example: <code>5</code>",
        parse_mode="HTML",
    )
    return PF_MIN_DAYS


async def _wiz_min_days(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = int(update.message.text.strip())
        assert v >= 0
    except Exception:
        await update.message.reply_text("⚠️ <b>Invalid Input</b>\n\nEnter a whole number. Example: <code>5</code>", parse_mode="HTML")
        return PF_MIN_DAYS
    _wizard_data["min_profit_days"] = v
    await update.message.reply_text(
        "<b>Step 9/10 — Consistency Rule</b>\n\n"
        "When the largest profitable day falls below this percentage of total profit, "
        "the system will halt and prompt payout submission.\n\n"
        "Common target: largest day &lt; 30% of total profit.\n\n"
        "⚠️ Enter the firm's stated limit WITHOUT buffer.\n"
        "The system will subtract 1pp automatically.\n"
        "(e.g. firm says 30% → enter <code>30</code> → bot triggers at 29%)\n\n"
        "Enter a value between 2 and 50. Example: <code>30</code>",
        parse_mode="HTML",
    )
    return PF_CONSISTENCY


async def _wiz_consistency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert 2.0 <= v <= 50.0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\n"
            "Enter a number between 2 and 50.\n"
            "Example: <code>30</code>",
            parse_mode="HTML",
        )
        return PF_CONSISTENCY
    _wizard_data["consistency_threshold_pct"] = v
    await update.message.reply_text(
        "<b>Step 10/10 — Initial Account Balance</b>\n\n"
        "Enter the prop firm's initial account balance (the starting balance the firm set for this evaluation).\n"
        "This is used as the static baseline for all kill condition calculations.\n\n"
        "Example: <code>100000</code>",
        parse_mode="HTML",
    )
    return PF_INITIAL_BALANCE


async def _wiz_initial_balance(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip().replace(",", ""))
        assert v > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number. Example: <code>100000</code>",
            parse_mode="HTML",
        )
        return PF_INITIAL_BALANCE
    _wizard_data["initial_balance"] = v

    eff = _apply_buffers(_wizard_data)
    dd_flag = "  <b>[FLAGGED]</b>" if not _wizard_data["drawdown_is_static"] else ""
    rs_flag = "  <b>[FLAGGED]</b>" if not _wizard_data["raw_spread_account"] else ""
    daily_dd_amt = round(v * eff["max_drawdown_daily_pct"] / 100.0, 2)
    floor_amt    = round(v * (1.0 - eff["max_drawdown_overall_pct"] / 100.0), 2)
    cap_amt      = round(v * eff["daily_profit_cap_pct"] / 100.0, 2)
    target_lvl   = round(v * (1.0 + _wizard_data["profit_target_pct"] / 100.0), 2)
    cons_raw = _wizard_data["consistency_threshold_pct"]
    cons_eff = eff["consistency_threshold_pct"]
    summary = (
        f"📊 <b>Review Prop Firm Setup</b>\n\n"
        f"<b>Firm:</b> {_wizard_data['propfirm_name']}\n"
        f"<b>Initial Balance:</b> ${v:,.2f}\n"
        f"<b>Profit Target:</b> {_wizard_data['profit_target_pct']:.1f}%\n"
        f"<b>Max DD Overall:</b> {_wizard_data['max_drawdown_overall_pct']:.1f}% → enforced at <b>{eff['max_drawdown_overall_pct']:.1f}%</b> (no buffer — exact)\n"
        f"<b>Max DD Daily:</b> {_wizard_data['max_drawdown_daily_pct']:.1f}% → enforced at <b>{eff['max_drawdown_daily_pct']:.1f}%</b> (−1pp buffer)\n"
        f"<b>Drawdown Type:</b> {'Static' if _wizard_data['drawdown_is_static'] else 'Dynamic'}{dd_flag}\n"
        f"<b>Raw Spread Acct:</b> {'Yes' if _wizard_data['raw_spread_account'] else 'No'}{rs_flag}\n"
        f"<b>Profit Sharing:</b> {_wizard_data['profit_sharing_pct']:.1f}%\n"
        f"<b>Min Profit Days:</b> {_wizard_data['min_profit_days']}\n"
        f"<b>Consistency:</b> {cons_raw:.1f}% → enforced at <b>{cons_eff:.1f}%</b> (−1pp buffer)\n\n"
        f"<b>Kill Levels (static, based on ${v:,.0f} baseline)</b>\n"
        f"K1 Daily DD: −${daily_dd_amt:,.2f} from day-start\n"
        f"K2 Overall DD: equity ≤ ${floor_amt:,.2f}\n"
        f"K3 Daily Cap: +${cap_amt:,.2f} from day-start\n"
        f"K4 Profit Target: equity ≥ ${target_lvl:,.2f}\n"
        f"K5 Consistency: largest day &lt; {cons_eff:.1f}% of total <i>(Phase 2 only)</i>\n\n"
        f"Reply <b>YES</b> to save, or <b>NO</b> to cancel."
    )
    await update.message.reply_text(summary, parse_mode="HTML")
    return PF_CONFIRM


async def _wiz_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip().upper()
    if v == "NO":
        _wizard_data.clear()
        await update.message.reply_text("⚠️ <b>Cancelled</b>\n\nNo changes were saved.", parse_mode="HTML")
        return ConversationHandler.END
    if v != "YES":
        await update.message.reply_text(
            "Reply <b>YES</b> to save, or <b>NO</b> to cancel.",
            parse_mode="HTML",
        )
        return PF_CONFIRM

    eff = _apply_buffers(_wizard_data)

    # Capture old state before overwriting
    with _pf_lock:
        old_name     = _propfirm.get("propfirm_name", "—")
        old_baseline = _propfirm.get("baseline_equity", 0.0)

    # Baseline is the user-provided initial account balance — always static for the evaluation life.
    # day_start_equity is fetched live from MT5 (resets daily).
    baseline = _wizard_data.get("initial_balance", 0.0)
    day_start = baseline
    try:
        day_start = _query_equity(ZMQ_REQ_PROP, "")["balance"]
    except Exception:
        pass  # fall back to baseline if MT5 unavailable

    with _pf_lock:
        _propfirm.update({
            "propfirm_name":              _wizard_data["propfirm_name"],
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
            "day_start_equity":           day_start,
            "day_start_date_utc":         _propfirm_day(_sgt_now()),
            "k1_layer":                   0,  # reset staircase layers on new evaluation
            "k3_layer":                   0,
        })
        # Store raw Phase 1 values for /phase2 wizard (raw = before buffers, what the firm states)
        _propfirm.setdefault("phase_configs", {})["1"] = {
            "propfirm_name":              _wizard_data["propfirm_name"],
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
    layer_loss = round(baseline * dd_daily   / 100.0, 2) if baseline > 0 else 0.0
    layer_cap  = round(baseline * cap        / 100.0, 2) if baseline > 0 else 0.0
    overall_fl = round(baseline * (1 - dd_overall / 100.0), 2) if baseline > 0 else 0.0
    target_lvl = round(baseline * (1.0 + target_pct / 100.0), 2) if baseline > 0 else 0.0
    max_layers = round(dd_overall / dd_daily) if dd_daily > 0 else 0
    before_str = f"{old_name}  |  Baseline: ${old_baseline:,.2f}" if old_name != "—" else "No previous config"

    _wizard_data.clear()
    await update.message.reply_text(
        f"✅ <b>Prop Firm Config Saved</b>\n\n"
        f"<b>Before</b>\n{before_str}\n\n"
        f"<b>After</b>\n{_propfirm['propfirm_name']} | Baseline: <b>${baseline:,.2f}</b>\n\n"
        f"<b>Risk Levels — Prop Account</b>\n"
        f"K1 Layer 1/{max_layers} — floor: ${baseline - layer_loss:,.2f}  (layer ${layer_loss:,.2f})\n"
        f"K2 Overall floor: ${overall_fl:,.2f}\n"
        f"K3 Layer 1 — cap: ${baseline + layer_cap:,.2f}  (layer ${layer_cap:,.2f})\n"
        f"K4 Profit Target: equity ≥ ${target_lvl:,.2f}\n\n"
        f"<b>Next Step</b>\nSend /phase1 or /phase2 to continue.",
        parse_mode="HTML",
    )
    logger.info("Prop firm config updated — firm=%s  baseline=%.2f",
                _propfirm["propfirm_name"], baseline)
    return ConversationHandler.END


async def _wiz_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text(
        "⚠️ <b>Setup Cancelled — /changepropfirm</b>\n\n"
        "No changes were saved.\n"
        "Type /changepropfirm to start again.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ── Telegram commands ─────────────────────────────────────────────────────

async def _cmd_setbaseline(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Directly overwrite baseline_equity without re-running /changepropfirm."""
    if not _auth(update):
        return
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        with _pf_lock:
            cur = _propfirm.get("baseline_equity", 0.0)
        await update.message.reply_text(
            f"Usage: <code>/setbaseline 100000</code>\n\nCurrent baseline: <b>${cur:,.2f}</b>",
            parse_mode="HTML",
        )
        return
    try:
        new_baseline = float(text[1].replace(",", ""))
        assert new_baseline > 0
    except Exception:
        await update.message.reply_text("⚠️ <b>Invalid Amount</b>\n\nExample: <code>/setbaseline 100000</code>", parse_mode="HTML")
        return

    with _pf_lock:
        old = _propfirm.get("baseline_equity", 0.0)
        _propfirm["baseline_equity"] = new_baseline
        _propfirm["k1_layer"] = 0  # reset staircase layers when baseline changes
        _propfirm["k3_layer"] = 0
        _save_propfirm(_propfirm)
        dd_daily   = _propfirm.get("max_drawdown_daily_pct",  0.0)
        dd_overall = _propfirm.get("max_drawdown_overall_pct", 0.0)
        cap        = _propfirm.get("daily_profit_cap_pct",     0.0)
        target     = _propfirm.get("profit_target_pct",        0.0)

    k1 = round(new_baseline * dd_daily   / 100.0, 2) if dd_daily   > 0 else 0.0
    k2 = round(new_baseline * (1 - dd_overall / 100.0), 2) if dd_overall > 0 else 0.0
    k3 = round(new_baseline * cap        / 100.0, 2) if cap        > 0 else 0.0
    k4 = round(new_baseline * (1 + target / 100.0), 2) if target   > 0 else 0.0

    await update.message.reply_text(
        f"✅ <b>Baseline Updated</b>\n\n"
        f"Before: ${old:,.2f}\n"
        f"After:  <b>${new_baseline:,.2f}</b>\n\n"
        f"<b>New Kill Levels</b>\n"
        f"K1 Daily DD: −${k1:,.2f} from day-start\n"
        f"K2 Overall DD: equity ≤ ${k2:,.2f}\n"
        f"K3 Daily Cap: +${k3:,.2f} from day-start\n"
        f"K4 Profit Target: equity ≥ ${k4:,.2f}",
        parse_mode="HTML",
    )
    logger.info("Baseline manually updated: %.2f → %.2f", old, new_baseline)


async def _cmd_phase1(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    with _pf_lock:
        existing_baseline = _propfirm.get("baseline_equity", 0.0)

    with _state_lock:
        _phase_state["phase"] = 1
        _phase_state.pop("permanently_halted", None)
        _phase_state.pop("phase1_permanently_halted", None)  # backward compat
        _save_phase(_phase_state)

    # Only lock a new baseline if none exists — baseline is STATIC for the life of an evaluation.
    # Re-running /phase1 mid-evaluation (e.g. after /stop) must not overwrite the baseline.
    if existing_baseline <= 0:
        balance, err = await asyncio.to_thread(_lock_baseline_from_live)
        if err:
            await update.message.reply_text(
                f"⚠️ <b>Phase 1 Prepared — Baseline Missing</b>\n\n"
                f"Personal multiplier: ×{PHASE_MULT[1]:.2f}\n\n"
                f"<b>Issue</b>\nCould not fetch live prop balance:\n<code>{err}</code>\n\n"
                f"Baseline was not set. Run /phase1 again once MT5 is connected.",
                parse_mode="HTML",
            )
            logger.warning("Telegram /phase1: baseline lock failed: %s", err)
            return
        baseline = balance
        baseline_note = "locked from live MT5"
    else:
        baseline = existing_baseline
        baseline_note = "unchanged — use /changepropfirm to reset"

    await asyncio.to_thread(_dispatch_parameters)

    with _pf_lock:
        pf = dict(_propfirm)
    dd_daily   = pf.get("max_drawdown_daily_pct",   0.0)
    dd_overall = pf.get("max_drawdown_overall_pct",  0.0)
    cap        = pf.get("daily_profit_cap_pct",      0.0)
    target     = pf.get("profit_target_pct",         0.0)
    k1_layer   = int(pf.get("k1_layer", 0))
    layer_loss = round(baseline * dd_daily   / 100.0, 2) if dd_daily   > 0 and baseline > 0 else 0.0
    layer_cap  = round(baseline * cap        / 100.0, 2) if cap        > 0 and baseline > 0 else 0.0
    overall_fl = round(baseline * (1 - dd_overall / 100.0), 2) if dd_overall > 0 and baseline > 0 else 0.0
    target_lvl = round(baseline * (1.0 + target / 100.0), 2)   if target    > 0 and baseline > 0 else 0.0
    max_layers = round(dd_overall / dd_daily) if dd_daily > 0 else 0
    active_fl  = baseline - (k1_layer + 1) * layer_loss if layer_loss > 0 else 0.0

    await update.message.reply_text(
        f"🟢 <b>Phase 1 Active</b>\n\n"
        f"<b>Risk Mode</b>\nPersonal multiplier: ×{PHASE_MULT[1]:.2f}\n"
        f"Baseline: <b>${baseline:,.2f}</b> ({baseline_note})\n\n"
        f"<b>Risk Levels — Prop Account</b>\n"
        f"K1 Layer {k1_layer + 1}/{max_layers} — floor: <b>${active_fl:,.2f}</b>  (layer ${layer_loss:,.2f})\n"
        f"K2 Overall floor: ${overall_fl:,.2f}\n"
        f"K3 Daily cap: ${layer_cap:,.2f} above day-start  ({cap:.1f}% of baseline)\n"
        f"K4 Profit Target: equity ≥ ${target_lvl:,.2f}\n\n"
        f"<b>Next Step</b>\nSend /resume to allow new signals.",
        parse_mode="HTML",
    )
    logger.info("Telegram: phase set to 1  baseline=%.2f", baseline)


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
            f"Example: <code>2 4</code> | Range: 1–9",
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
            if 1 <= n <= 9 and n not in indices:
                indices.append(n)
        except ValueError:
            pass
    if not indices:
        await update.message.reply_text(
            "⚠️ <b>No Valid Settings Selected</b>\n\n"
            "Enter numbers 1–9 separated by spaces.\n"
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
        f"<b>Risk Controls</b>\n"
        f"Kill 1 — daily loss ≥ {eff['max_drawdown_daily_pct']:.1f}%\n"
        f"Kill 2 — overall loss ≥ {eff['max_drawdown_overall_pct']:.1f}%\n"
        f"Kill 3 — daily profit ≥ {eff['daily_profit_cap_pct']:.1f}%\n"
        f"Kill 4 — overall profit ≥ {new['profit_target_pct']:.1f}%\n"
        f"Kill 5 — consistency: largest day &lt; {eff['consistency_threshold_pct']:.1f}% of total → permanent halt\n"
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
        await update.message.reply_text("⚠️ <b>Cancelled</b>\n\nNo changes were saved.", parse_mode="HTML")
        return ConversationHandler.END
    if v != "YES":
        await update.message.reply_text("Reply <b>YES</b> to proceed, or <b>NO</b> to cancel.", parse_mode="HTML")
        return P2_INITIAL_BALANCE
    await update.message.reply_text(
        "<b>Initial Account Balance</b>\n\n"
        "Enter the prop firm's initial account balance for this Phase 2 challenge.\n"
        "This becomes the static baseline for all kill condition calculations.\n\n"
        "Example: <code>200000</code>",
        parse_mode="HTML",
    )
    return P2_CONFIRM


async def _p2_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v_bal = float(update.message.text.strip().replace(",", ""))
        assert v_bal > 0
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Input</b>\n\nEnter a positive number. Example: <code>200000</code>",
            parse_mode="HTML",
        )
        return P2_CONFIRM

    new = _p2_wizard_data["new_config"]
    eff = _apply_buffers(new)

    with _pf_lock:
        old_baseline = _propfirm.get("baseline_equity", 0.0)

    # baseline = user-provided initial balance (static for evaluation life)
    # day_start = live MT5 equity (resets daily)
    baseline = v_bal
    day_start = baseline
    try:
        day_start = _query_equity(ZMQ_REQ_PROP, "")["balance"]
    except Exception:
        pass

    today = _propfirm_day(_sgt_now())
    cons_threshold = eff["consistency_threshold_pct"]
    with _pf_lock:
        _propfirm.update({
            "propfirm_name":              new["propfirm_name"],
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
            "day_start_equity":           day_start,
            "day_start_date_utc":         today,
            "k1_layer":                   0,  # reset staircase layers on new evaluation
            "k3_layer":                   0,
        })
        # Store raw Phase 2 config for future reference
        _propfirm.setdefault("phase_configs", {})["2"] = {k: new[k] for k in new}
        _save_propfirm(_propfirm)

    # Reset consistency log — fresh start for this funded cycle
    _reset_consistency_log()

    with _state_lock:
        _phase_state["phase"] = 2
        _phase_state.pop("permanently_halted", None)
        _phase_state.pop("phase1_permanently_halted", None)  # backward compat
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
        f"<b>Firm</b>\n{_propfirm['propfirm_name']}\n\n"
        f"<b>Risk Mode</b>\nPersonal multiplier: ×{PHASE_MULT[2]:.2f}\n"
        f"Baseline before: ${old_baseline:,.2f}\n"
        f"Baseline now: <b>${baseline:,.2f}</b> (locked from live MT5)\n\n"
        f"<b>Risk Levels — Prop Account</b>\n"
        f"K1 Daily DD: −${daily_dd_amt:,.2f} from day-start\n"
        f"K2 Overall DD: equity ≤ <b>${floor_amt:,.2f}</b>\n"
        f"K3 Daily Cap: +${cap_amt:,.2f} from day-start\n"
        f"K4 Profit Target: equity ≥ ${target_lvl:,.2f}\n\n"
        f"<b>Next Step</b>\nSend /resume to allow new signals.",
        parse_mode="HTML",
    )
    logger.info("Phase 2 started — firm=%s  baseline=%.2f", _propfirm["propfirm_name"], baseline)
    return ConversationHandler.END


async def _p2_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _p2_wizard_data.clear()
    await update.message.reply_text(
        "⚠️ <b>Setup Cancelled — /phase2</b>\n\n"
        "No changes were saved.\n"
        "Type /phase2 to start again.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    # Capture state BEFORE halting so user knows what's still running
    try:
        prop_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
    except Exception:
        prop_pos = []
    try:
        pers_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS)
    except Exception:
        pers_pos = []
    try:
        prop_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        pers_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        eq_lines = [
            f"Prop: Balance ${prop_eq['balance']:,.2f} | Equity: ${prop_eq['equity']:,.2f}",
            f"Personal: Balance ${pers_eq['balance']:,.2f} | Equity: ${pers_eq['equity']:,.2f}",
        ]
    except Exception:
        eq_lines = ["Could not query equity"]

    with _state_lock:
        _phase_state["active"] = False
        _save_phase(_phase_state)

    lines: list[str] = ["⚠️ <b>Signal Processing Halted</b>\n", "<b>Open Positions at Halt</b>"]
    total_open = 0
    for label, positions in [
        ("Personal (VPS #3)", pers_pos),
        ("Prop (VPS #2)",     prop_pos),
    ]:
        lines.append(f"<b>{label}:</b>")
        if positions:
            for p in positions:
                d = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                lines.append(
                    f"  {p['symbol']} {d} {p['volume']:.2f} lots"
                    f"  P&amp;L: ${p['profit']:+,.2f}"
                )
                total_open += 1
        else:
            lines.append("  No open positions")

    lines.append("\n<b>Live Equity</b>\n" + "\n".join(eq_lines))

    if total_open > 0:
        lines.append(
            f"\n⚠️ <b>{total_open} position(s) still open</b>\n"
            f"They will continue to SL/TP naturally. Use /emergency to force-close immediately."
        )
    lines.append("\n<b>Next Step</b>\nSend /resume to re-enable signal processing.")
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
    with _state_lock:
        _phase_state["active"] = True
        _save_phase(_phase_state)

    # Capture state AFTER resuming so user sees what they're resuming into
    try:
        prop_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
    except Exception:
        prop_pos = []
    try:
        pers_pos = await asyncio.to_thread(_query_positions, ZMQ_REQ_PERS)
    except Exception:
        pers_pos = []
    try:
        prop_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        pers_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        eq_lines = [
            f"Prop: Balance ${prop_eq['balance']:,.2f} | Equity: ${prop_eq['equity']:,.2f}",
            f"Personal: Balance ${pers_eq['balance']:,.2f} | Equity: ${pers_eq['equity']:,.2f}",
        ]
    except Exception:
        eq_lines = ["Could not query equity"]

    curfew_note = "\n<i>SGT curfew active — new signals start from 12:00 SGT.</i>" if _is_sgt_curfew() else ""
    lines: list[str] = [f"🟢 <b>Signal Processing Resumed</b>{curfew_note}\n", "<b>Current Open Positions</b>\n"]
    for label, positions in [
        ("Personal (VPS #3)", pers_pos),
        ("Prop (VPS #2)",     prop_pos),
    ]:
        lines.append(f"<b>{label}:</b>")
        if positions:
            for p in positions:
                d = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                lines.append(
                    f"  {p['symbol']} {d} {p['volume']:.2f} lots"
                    f"  P&amp;L: ${p['profit']:+,.2f}"
                )
        else:
            lines.append("  No open positions")

    lines.append("\n<b>Live Equity</b>\n" + "\n".join(eq_lines))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    logger.info("Telegram: resumed by user")


async def _cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        phase   = _phase_state.get("phase", "?")
        active  = _phase_state.get("active", False)
        last_ts = _phase_state.get("last_signal_ts", "never")
        p_halt  = _phase_state.get("permanently_halted", False)
        max_pos = _phase_state.get("max_open_positions", 2)
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
    await update.message.reply_text(
        f"📊 <b>System Status</b>\n\n"
        f"<b>Trading</b>\n"
        f"Phase: {phase} (×{mult})\n"
        f"Status: {'🟢 Active' if active else '⚠️ Halted'}\n"
        f"Permanent halt: {'🔴 Yes — /phase2 required' if p_halt else 'No'}\n"
        f"Curfew: {'Yes — dormant' if curfew else 'No'}\n"
        f"Window: {win_curr['start']}–{win_curr['end']} SGT"
        + (f" | Next: {win_next['start']}–{win_next['end']}" if win_next else "") + "\n"
        f"Max positions: {max_pos}\n\n"
        f"<b>Prop Firm</b>\n{pf_name}\n\n"
        f"<b>Risk Snapshot</b>\n"
        f"Baseline: ${baseline:,.2f}\n"
        f"DD floor: ${floor:,.2f} (−{dd_overall:.1f}%)\n"
        f"Day-start: ${day_start:,.2f}\n"
        f"Daily DD: {dd_daily:.1f}% | Daily cap: {cap:.1f}%\n\n"
        f"<b>Last Signal</b>\n{last_ts_display}",
        parse_mode="HTML",
    )


async def _cmd_propfirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _pf_lock:
        pf = dict(_propfirm)
    await update.message.reply_text(
        f"📊 <b>Prop Firm Configuration</b>\n\n"
        f"<b>Firm:</b> {pf.get('propfirm_name', '—')}\n"
        f"<b>Profit Target:</b> {pf.get('profit_target_pct', 0):.1f}%\n"
        f"<b>Max DD Overall:</b> {pf.get('max_drawdown_overall_pct', 0):.1f}%  (no buffer — exact firm limit)\n"
        f"<b>Max DD Daily:</b> {pf.get('max_drawdown_daily_pct', 0):.1f}%  (−1pp buffer applied)\n"
        f"<b>Drawdown Type:</b> {'Static' if pf.get('drawdown_is_static') else 'Dynamic'}\n"
        f"<b>Raw Spread Acct:</b> {'Yes' if pf.get('raw_spread_account') else 'No'}\n"
        f"<b>Profit Sharing:</b> {pf.get('profit_sharing_pct', 0):.1f}%\n"
        f"<b>Min Profit Days:</b> {pf.get('min_profit_days', 0)}\n"
        f"<b>Daily Profit Cap:</b> {pf.get('daily_profit_cap_pct', 0):.1f}%\n"
        f"<b>Baseline Equity:</b> ${pf.get('baseline_equity', 0):,.2f}\n"
        f"<b>Day-Start Equity:</b> ${pf.get('day_start_equity', 0):,.2f}",
        parse_mode="HTML",
    )


async def _cmd_equity(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _pf_lock:
        pf = dict(_propfirm)
    baseline  = pf.get("baseline_equity",  0.0)
    day_start = pf.get("day_start_equity", 0.0)

    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        eq = prop["equity"]
        daily_pnl   = eq - day_start if day_start > 0 else 0.0
        overall_pnl = eq - baseline  if baseline  > 0 else 0.0
        daily_pct   = daily_pnl   / baseline * 100 if baseline > 0 else 0.0
        overall_pct = overall_pnl / baseline * 100 if baseline > 0 else 0.0
        d_arrow = "↑" if daily_pnl   >= 0 else "↓"
        o_arrow = "↑" if overall_pnl >= 0 else "↓"
        prop_text = (
            f"Balance: ${prop['balance']:,.2f} | Equity: ${eq:,.2f}\n"
            f"{d_arrow} Today: ${daily_pnl:+,.2f} ({daily_pct:+.2f}%)"
            f"  {o_arrow} Overall: ${overall_pnl:+,.2f} ({overall_pct:+.2f}%)"
        )
    except Exception as exc:
        prop_text = f"OFFLINE — {exc}"
    try:
        pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_text = f"Balance: ${pers['balance']:,.2f} | Equity: ${pers['equity']:,.2f}"
    except Exception as exc:
        pers_text = f"OFFLINE — {exc}"
    await update.message.reply_text(
        f"📊 <b>Live Equity Snapshot</b>\n\n"
        f"<b>Prop (VPS #2):</b>\n{prop_text}\n\n"
        f"<b>Personal (VPS #3):</b>\n{pers_text}",
        parse_mode="HTML",
    )


async def _cmd_emergency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END

    await update.message.reply_text("Checking open positions…")

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

    lines = ["🔴 <b>Emergency Halt — Position Summary</b>\n"]

    total_open = 0
    for label, positions, err in [
        ("Personal (VPS #3)", pers_pos, pers_err),
        ("Prop (VPS #2)", prop_pos, prop_err),
    ]:
        lines.append(f"<b>{label}:</b>")
        if err:
            lines.append(f"  OFFLINE — {err}")
        elif not positions:
            lines.append("  No open positions")
        else:
            for p in positions:
                direction = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                lines.append(
                    f"  {p['symbol']}  {direction}  {p['volume']:.2f} lots"
                    f"  |  P&amp;L: ${p['profit']:+,.2f}"
                )
                total_open += 1
        lines.append("")

    if total_open == 0:
        lines.append("No positions are currently open on either account.")

    lines.append("⚠️ <b>Confirmation Required</b>")
    lines.append("Reply <code>CONFIRM</code> to force-close all positions and halt signal processing.")
    lines.append("Send /cancel to abort.")

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
    await asyncio.sleep(2)  # let MT5 execute the close before querying
    try:
        prop_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        pers_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        eq_text = (
            f"Prop: Balance ${prop_eq['balance']:,.2f} | Equity: ${prop_eq['equity']:,.2f}\n"
            f"Personal: Balance ${pers_eq['balance']:,.2f} | Equity: ${pers_eq['equity']:,.2f}"
        )
    except Exception:
        eq_text = "Could not query equity"
    await update.message.reply_text(
        f"🔴 <b>Emergency Halt Executed</b>\n\n"
        f"<b>Action Taken</b>\n"
        f"All positions force-closed on both MT5 accounts.\n"
        f"Signal processing halted.\n\n"
        f"<b>Equity After Close</b>\n{eq_text}\n\n"
        f"<b>Next Step</b>\nSend /resume only after confirming both accounts are safe.",
        parse_mode="HTML",
    )
    logger.warning("Telegram: emergency halt executed by user")
    return ConversationHandler.END


async def _emergency_abort(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "⚠️ <b>Emergency Cancelled</b>\n\n"
        "No positions were closed.\n"
        "Type /emergency to try again.",
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

    lines = ["📊 <b>Open Positions</b>\n"]
    for label, positions, err in [
        ("Personal — signal direction (VPS #3)", pers_pos, pers_err),
        ("Prop — inverse direction (VPS #2)", prop_pos, prop_err),
    ]:
        lines.append(f"<b>{label}:</b>")
        if err:
            lines.append(f"OFFLINE — {err}")
        elif not positions:
            lines.append("No open positions")
        else:
            for p in positions:
                direction = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                lines.append(
                    f"{p['symbol']}  {direction}  {p['volume']:.2f} lots\n"
                    f"  Entry: {p['price_open']}  SL: {p['sl']}  TP: {p['tp']}\n"
                    f"  P&amp;L: ${p['profit']:+,.2f}"
                )
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
    k1_layer    = int(pf.get("k1_layer", 0))
    equity      = prop["equity"]

    overall_pnl    = equity - baseline
    daily_pnl      = equity - day_start
    layer_loss_amt = round(baseline * daily_dd   / 100.0, 2) if daily_dd   > 0 else 0.0
    daily_cap_amt  = round(baseline * daily_cap  / 100.0, 2) if daily_cap  > 0 else 0.0
    overall_dd_amt = round(baseline * overall_dd / 100.0, 2) if overall_dd > 0 else 0.0
    target_amt     = round(baseline * target_pct / 100.0, 2) if target_pct > 0 else 0.0
    max_loss_layers = round(overall_dd / daily_dd) if daily_dd > 0 else 0

    overall_floor  = baseline - overall_dd_amt
    active_floor   = baseline - (k1_layer + 1) * layer_loss_amt if layer_loss_amt > 0 else 0.0
    daily_cap_level = day_start + daily_cap_amt
    k4_target      = baseline + target_amt
    daily_remaining = max(0.0, daily_cap_level - equity)

    # K1 bar: % consumed within the current active loss layer
    k1_prev_floor = baseline - k1_layer * layer_loss_amt
    k1_consumed   = max(0.0, k1_prev_floor - equity)
    k1_bar_pct    = k1_consumed / layer_loss_amt * 100 if layer_loss_amt > 0 else 0.0
    k1_status     = "🔴 BREACHED" if equity <= active_floor else "🟢 Active"

    # K2 bar: % of overall DD consumed from baseline
    k2_consumed = max(0.0, baseline - equity)
    k2_bar_pct  = k2_consumed / overall_dd_amt * 100 if overall_dd_amt > 0 else 0.0

    # K3 bar: % of daily cap consumed today
    k3_consumed = max(0.0, equity - day_start)
    k3_bar_pct  = k3_consumed / daily_cap_amt * 100 if daily_cap_amt > 0 else 0.0

    # K4 bar: % progress toward overall profit target
    k4_bar_pct  = max(0.0, overall_pnl) / target_amt * 100 if target_amt > 0 else 0.0

    lines = [
        f"📊 <b>P&amp;L Dashboard — Prop Account</b>\n",
        f"Baseline: ${baseline:,.2f}",
        f"Day started: ${day_start:,.2f}",
        f"Now: ${equity:,.2f}",
        f"Daily P&amp;L: <b>${daily_pnl:+,.2f}</b>",
        f"Overall P&amp;L: <b>${overall_pnl:+,.2f}</b>",
    ]
    if layer_loss_amt > 0 and max_loss_layers > 0:
        lines += [
            f"\n<b>K1/K2 — Loss Protection</b>",
            f"Layer size: ${layer_loss_amt:,.2f} ({daily_dd:.1f}% of baseline)",
            f"Active layer: {k1_layer + 1}/{max_loss_layers}  |  {k1_status}",
            f"Active floor: <b>${active_floor:,.2f}</b>",
            f"Overall DD floor: ${overall_floor:,.2f}",
            f"<code>{_pnl_bar(k1_bar_pct)}</code>  {k1_bar_pct:.1f}% toward floor",
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
        f"Prop Worker (VPS #2): {prop_h}\n"
        f"Personal Worker (VPS #3): {pers_h}",
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
        await update.message.reply_text("📰 <b>News Check</b>\n\nNo high-impact events in the next 4 hours for covered pairs.", parse_mode="HTML")
        return

    lines = ["📰 <b>Upcoming High-Impact News</b>\n<i>Next 4 hours | Covered pairs only</i>\n"]
    for t, ccy, title, pairs in relevant:
        sgt_str   = (t + sgt_off).strftime("%H:%M SGT")
        pairs_str = ", ".join(pairs) if pairs else "—"
        lines.append(f"🟠 {sgt_str} — {ccy}: {title}\n  Affects: {pairs_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
        await update.message.reply_text("🟢 <b>Blackboard Clear</b>\n\nNo pairs are currently suppressed.", parse_mode="HTML")
        return

    for ticker in sorted(all_pairs):
        lines.append(f"🔴 <b>{ticker}</b>")
        if ticker in manual_active:
            lines.append(f"  Manually blocked via /closepair")
            lines.append(f"  Unblock: /resumepair {ticker}")
        if ticker in news_active:
            ends_sgt = (news_active[ticker] + sgt_off).strftime("%H:%M SGT")
            lines.append(f"  News suppression — signals blocked until {ends_sgt}")
            lines.append(f"  Unblocks automatically after news window")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
    await update.message.reply_text(f"Checking {ticker} positions…")

    broker_symbol = _SYMBOL_MAP.get(ticker, ticker)
    lines = [f"⚠️ <b>Close Pair — {ticker}</b>\n"]
    total_open = 0
    for label, url in [("Personal (VPS #3)", ZMQ_REQ_PERS), ("Prop (VPS #2)", ZMQ_REQ_PROP)]:
        try:
            positions = await asyncio.to_thread(_query_positions, url)
            pair_pos = [p for p in positions if p["symbol"] in (ticker, broker_symbol)]
            lines.append(f"<b>{label}:</b>")
            if pair_pos:
                for p in pair_pos:
                    arrow = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                    lines.append(
                        f"  {p['symbol']} {arrow} {p['volume']:.2f} lots"
                        f"  |  P&amp;L: ${p['profit']:+,.2f}"
                    )
                    total_open += 1
            else:
                lines.append("  No open positions")
        except Exception as exc:
            lines.append(f"<b>{label}:</b>\n  OFFLINE — {exc}")
        lines.append("")

    if total_open == 0:
        lines.append(f"No {ticker} positions are currently open on either account.")
    lines.append("<b>Confirmation Required</b>")
    lines.append(f"Reply <code>CONFIRM</code> to close all {ticker} positions and block new signals.")
    lines.append("Send /cancel to abort.")

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
    await asyncio.sleep(2)  # let MT5 execute the close before querying
    try:
        prop_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        pers_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        eq_text = (
            f"Prop: Balance ${prop_eq['balance']:,.2f} | Equity: ${prop_eq['equity']:,.2f}\n"
            f"Personal: Balance ${pers_eq['balance']:,.2f} | Equity: ${pers_eq['equity']:,.2f}"
        )
    except Exception:
        eq_text = "Could not query equity"
    await update.message.reply_text(
        f"⚠️ <b>Pair Closed and Blocked — {ticker}</b>\n\n"
        f"<b>Action Taken</b>\n"
        f"All {ticker} positions closed on both accounts.\n"
        f"New {ticker} signals are blocked.\n\n"
        f"<b>Equity After Close</b>\n{eq_text}\n\n"
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
        f"⚠️ <b>Close Pair Cancelled — {ticker}</b>\n\n"
        f"No positions were closed.\n"
        f"Type /closepair {ticker} to try again.",
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
        f"🟢 <b>Pair Resumed — {ticker}</b>\n\nNew {ticker} signals will now be accepted.",
        parse_mode="HTML",
    )
    logger.info("Manual resumepair: %s", ticker)


async def _cmd_setmaxpos(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        await update.message.reply_text(
            "Usage: <code>/setmaxpos &lt;number&gt;</code>\n"
            "Example: <code>/setmaxpos 2</code>\n"
            "Range: 1–10",
            parse_mode="HTML",
        )
        return
    try:
        n = int(text[1])
        assert 1 <= n <= 10
    except Exception:
        await update.message.reply_text("⚠️ <b>Invalid Limit</b>\n\nEnter a whole number between 1 and 10.", parse_mode="HTML")
        return

    with _state_lock:
        old_max = _phase_state.get("max_open_positions", 2)
        _phase_state["max_open_positions"] = n
        _save_phase(_phase_state)

    warning = ""
    if n > 5:
        theoretical = round(n * PROP_RISK_PCT * 100, 2)
        with _pf_lock:
            dd_daily_raw = _propfirm.get("max_drawdown_daily_pct", 0.0) + 1.0  # before buffer
        warning = (
            f"\n\n⚠️ <b>Warning:</b> {n} positions × {PROP_RISK_PCT*100:.1f}% = "
            f"<b>{theoretical:.1f}% theoretical max daily loss</b> if all SLs hit simultaneously.\n"
            f"Daily DD limit (before buffer): {dd_daily_raw:.1f}%"
        )

    await update.message.reply_text(
        f"📊 <b>Max Position Limit Updated</b>\n\n"
        f"Before: {old_max}\n"
        f"After: <b>{n}</b>{warning}",
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
        f"Open positions: {count_str}/{limit}",
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
            "Status: Not active\n"
            "Requirement: Phase 2 only\n\n"
            "Run /phase2 to start the funded phase.",
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
    firm      = pf.get("propfirm_name",              "—")

    with _cons_lock:
        locked_days = list(_consistency_log.get("days", []))

    today_date    = _propfirm_day(_sgt_now())
    today_running = prop_equity - day_start if day_start > 0 else 0.0

    table_str, total, max_day_val, ratio_pct, rule_met = _build_consistency_table(
        locked_days, today_running, today_date, baseline, threshold,
    )

    if rule_met:
        status_line = f"🟢 <b>RULE MET</b> — ready to submit payout claim to {firm}"
    else:
        days_with_profit = len(locked_days) + (1 if today_running > 0 else 0)
        if days_with_profit < 2:
            status_line = "Need at least 2 profitable days to evaluate."
        else:
            status_line = f"Not met yet — largest day is {ratio_pct:.1f}% of total profit (need &lt; {threshold:.1f}%)."

    await update.message.reply_text(
        f"📊 <b>Consistency Tracker</b>\n"
        f"Phase 2  ·  {firm}  ·  Threshold: &lt; {threshold:.0f}%\n\n"
        f"<pre>{table_str}</pre>\n\n"
        f"{status_line}",
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
    await update.message.reply_text("⚠️ <b>Trading Window Update Cancelled</b>\n\nNo changes were applied.", parse_mode="HTML")
    return ConversationHandler.END


async def _cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    await update.message.reply_text(
        "<b>HedgeHog — Command Reference</b>\n\n"

        "<b>Emergency</b>\n"
        "/emergency — Force-close ALL positions on both accounts + halt\n\n"

        "<b>System Status</b>\n"
        "/health — Connectivity across all 4 layers\n"
        "/status — Phase, active flag, last signal time\n"
        "/positions — Open trades on both accounts\n"
        "/equity — Live balance and equity\n"
        "/pnl — Daily and overall P&amp;L vs cap and DD limits\n\n"

        "<b>Trading Control</b>\n"
        "/resume — Resume signal processing after halt\n"
        "/stop — Halt new signals (open trades run to SL/TP)\n"
        "/phase1 — Start Phase 1 (×0.20 lots) + run prop firm wizard\n"
        "/phase2 — Start Phase 2 (×0.70 lots) + review/update settings\n\n"

        "<b>Position Management</b>\n"
        "/maxpos — Current open trade limit and live count\n"
        "/setmaxpos N — Set max simultaneous open trades (1–10)\n"
        "/closepair EURUSD — Close pair + block new signals\n"
        "/resumepair EURUSD — Unblock pair\n\n"

        "<b>Risk Monitoring</b>\n"
        "/news — High-impact events in the next 4h\n"
        "/blackboard — Active manual and news blocks\n"
        "/consistency — Consistency rule tracker (Phase 2 only)\n\n"

        "<b>Configuration</b>\n"
        "/propfirm — Current prop firm settings\n"
        "/changepropfirm — Update prop firm (9-step wizard)\n"
        "/setwindow HH:MM HH:MM — Update trading window\n"
        "/cancel — Cancel any active wizard\n\n"

        "<b>Kill Conditions</b> (automatic)\n"
        "K1 — Daily loss ≥ DD limit → close all + halt\n"
        "K2 — Overall loss ≥ DD limit → close all + permanent halt\n"
        "K3 — Daily profit ≥ cap → close all + halt\n"
        "K4 — Overall profit ≥ target → permanent halt → /phase2\n"
        "K5 — Consistency rule met → permanent halt → claim payout\n\n"

        "<b>First-Time Setup</b>\n"
        "/changepropfirm → /phase1 → /resume",
        parse_mode="HTML",
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
            PF_CONSISTENCY:      [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_consistency)],
            PF_INITIAL_BALANCE:  [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_initial_balance)],
            PF_CONFIRM:          [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_confirm)],
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

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(wizard)
    tg_app.add_handler(p2_wizard)
    tg_app.add_handler(emergency_wizard)
    tg_app.add_handler(closepair_wizard)
    tg_app.add_handler(setwindow_wizard)
    tg_app.add_handler(CommandHandler("phase1",        _cmd_phase1))
    tg_app.add_handler(CommandHandler("setbaseline",   _cmd_setbaseline))
    tg_app.add_handler(CommandHandler("stop",          _cmd_stop))
    tg_app.add_handler(CommandHandler("resume",        _cmd_resume))
    tg_app.add_handler(CommandHandler("status",        _cmd_status))
    tg_app.add_handler(CommandHandler("propfirm",      _cmd_propfirm))
    tg_app.add_handler(CommandHandler("equity",        _cmd_equity))
    tg_app.add_handler(CommandHandler("changepropfirm", _cmd_changepropfirm))
    tg_app.add_handler(CommandHandler("positions",     _cmd_positions))
    tg_app.add_handler(CommandHandler("pnl",           _cmd_pnl))
    tg_app.add_handler(CommandHandler("health",        _cmd_health))
    tg_app.add_handler(CommandHandler("news",          _cmd_news))
    tg_app.add_handler(CommandHandler("blackboard",    _cmd_blackboard))
    tg_app.add_handler(CommandHandler("resumepair",    _cmd_resumepair))
    tg_app.add_handler(CommandHandler("setmaxpos",     _cmd_setmaxpos))
    tg_app.add_handler(CommandHandler("maxpos",        _cmd_maxpos))
    tg_app.add_handler(CommandHandler("consistency",   _cmd_consistency))
    tg_app.add_handler(CommandHandler("help",          _cmd_help))
    tg_app.add_handler(CommandHandler("setwindow",     _cmd_setwindow))

    async def _poll():
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=["message"])
        logger.info("Telegram bot polling (chat_id=%d)", CHAT_ID)
        await asyncio.Event().wait()  # block forever; thread is daemon so exits with process

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_poll())
