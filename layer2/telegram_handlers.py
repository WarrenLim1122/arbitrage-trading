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
 PF_CONSISTENCY, PF_CONFIRM) = range(10)

(P2_SAME_OR_DIFF, P2_WHICH_FIELDS, P2_COLLECTING, P2_CONFIRM) = range(10, 14)

EMERGENCY_CONFIRM  = 14
CLOSEPAIR_CONFIRM  = 15

_wizard_data: dict = {}
_p2_wizard_data: dict = {}


def _auth(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == CHAT_ID


async def _cmd_changepropfirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text(
        "<b>Change Prop Firm Config</b>\n\n"
        "<b>Step 1 of 8</b> — Enter the prop firm name:",
        parse_mode="HTML",
    )
    return PF_NAME


async def _wiz_name(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _wizard_data["propfirm_name"] = update.message.text.strip()
    await update.message.reply_text(
        "<b>Step 2 of 8</b> — Profit Target %\n\nEnter the firm's profit target (e.g. <code>10</code>):",
        parse_mode="HTML",
    )
    return PF_PROFIT_TARGET


async def _wiz_profit_target(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("Invalid — enter a positive number (e.g. <code>10</code>):", parse_mode="HTML")
        return PF_PROFIT_TARGET
    _wizard_data["profit_target_pct"] = v
    await update.message.reply_text(
        "<b>Step 3 of 8</b> — Max Drawdown Overall %\n\nEnter the firm's raw overall DD limit (e.g. <code>10</code>):",
        parse_mode="HTML",
    )
    return PF_MAX_DD_OVERALL


async def _wiz_max_dd_overall(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("Invalid — enter a positive number (e.g. <code>10</code>):", parse_mode="HTML")
        return PF_MAX_DD_OVERALL
    _wizard_data["max_drawdown_overall_pct"] = v
    await update.message.reply_text(
        "<b>Step 4 of 8</b> — Max Drawdown Daily %\n\nEnter the firm's raw daily DD limit (e.g. <code>3</code>):",
        parse_mode="HTML",
    )
    return PF_MAX_DD_DAILY


async def _wiz_max_dd_daily(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert v > 0
    except Exception:
        await update.message.reply_text("Invalid — enter a positive number (e.g. <code>3</code>):", parse_mode="HTML")
        return PF_MAX_DD_DAILY
    _wizard_data["max_drawdown_daily_pct"] = v
    await update.message.reply_text(
        "<b>Step 5 of 8</b> — Drawdown Type\n\nType <code>static</code> or <code>dynamic</code>:",
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
                "Dynamic drawdown accepted (flagged).\n\n"
                "<b>Step 6 of 8</b> — Raw Spread Account\n\nType <code>yes</code> or <code>no</code>:",
                parse_mode="HTML",
            )
            return PF_RAW_SPREAD
        else:
            _wizard_data.pop("_dd_type_confirming")
            await update.message.reply_text(
                "Confirmation not received. Re-enter drawdown type:\n\n"
                "<code>static</code>  or  <code>dynamic</code>",
                parse_mode="HTML",
            )
            return PF_DD_TYPE

    v_lower = v.lower()
    if v_lower == "static":
        _wizard_data["drawdown_is_static"] = True
        await update.message.reply_text(
            "<b>Step 6 of 8</b> — Raw Spread Account\n\nType <code>yes</code> or <code>no</code>:",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD
    elif v_lower == "dynamic":
        _wizard_data["_dd_type_confirming"] = True
        await update.message.reply_text(
            "<b>Warning — Dynamic Drawdown Flagged</b>\n\n"
            "This system is designed for static drawdown accounts.\n"
            "Reply <b>CONFIRM</b> to accept, or type <code>static</code> to correct.",
            parse_mode="HTML",
        )
        return PF_DD_TYPE
    else:
        await update.message.reply_text(
            "Type exactly: <code>static</code>  or  <code>dynamic</code>",
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
                "Non-raw spread accepted (flagged).\n\n"
                "<b>Step 7 of 8</b> — Profit Sharing %\n\nEnter the profit sharing % (e.g. <code>80</code>):",
                parse_mode="HTML",
            )
            return PF_PROFIT_SHARE
        else:
            _wizard_data.pop("_raw_spread_confirming")
            await update.message.reply_text(
                "Confirmation not received. Re-enter:\n\n"
                "<code>yes</code>  or  <code>no</code>",
                parse_mode="HTML",
            )
            return PF_RAW_SPREAD

    v_lower = v.lower()
    if v_lower == "yes":
        _wizard_data["raw_spread_account"] = True
        await update.message.reply_text(
            "<b>Step 7 of 8</b> — Profit Sharing %\n\nEnter the profit sharing % (e.g. <code>80</code>):",
            parse_mode="HTML",
        )
        return PF_PROFIT_SHARE
    elif v_lower == "no":
        _wizard_data["_raw_spread_confirming"] = True
        await update.message.reply_text(
            "<b>Warning — Non-Raw Spread Account Flagged</b>\n\n"
            "This system is designed for raw spread accounts.\n"
            "Reply <b>CONFIRM</b> to accept, or type <code>yes</code> to correct.",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD
    else:
        await update.message.reply_text(
            "Type exactly: <code>yes</code>  or  <code>no</code>",
            parse_mode="HTML",
        )
        return PF_RAW_SPREAD


async def _wiz_profit_share(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert 0 < v <= 100
    except Exception:
        await update.message.reply_text("Invalid — enter a number between 1 and 100:", parse_mode="HTML")
        return PF_PROFIT_SHARE
    _wizard_data["profit_sharing_pct"] = v
    await update.message.reply_text(
        "<b>Step 8 of 8</b> — Minimum Profit Days\n\nEnter the minimum trading days required (e.g. <code>5</code>):",
        parse_mode="HTML",
    )
    return PF_MIN_DAYS


async def _wiz_min_days(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = int(update.message.text.strip())
        assert v >= 0
    except Exception:
        await update.message.reply_text("Invalid — enter a whole number (e.g. 5):")
        return PF_MIN_DAYS
    _wizard_data["min_profit_days"] = v
    await update.message.reply_text(
        "<b>Step 9 of 9</b> — Consistency Rule Threshold\n\n"
        "When the largest single profitable day falls below this % of total profit, "
        "the system halts and prompts you to submit a payout claim.\n\n"
        "Most prop firms require the largest day &lt; 30% of total profit.\n"
        "We default to <b>29%</b> as a 1% safety buffer.\n\n"
        "Enter a % between 1 and 50, or reply <code>29</code> to use the default:",
        parse_mode="HTML",
    )
    return PF_CONSISTENCY


async def _wiz_consistency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        v = float(update.message.text.strip())
        assert 1.0 <= v <= 50.0
    except Exception:
        await update.message.reply_text(
            "Invalid — enter a number between 1 and 50 (e.g. <code>29</code>):",
            parse_mode="HTML",
        )
        return PF_CONSISTENCY
    _wizard_data["consistency_threshold_pct"] = v

    eff = _apply_buffers(_wizard_data)
    dd_flag = "  <b>[FLAGGED]</b>" if not _wizard_data["drawdown_is_static"] else ""
    rs_flag = "  <b>[FLAGGED]</b>" if not _wizard_data["raw_spread_account"] else ""
    summary = (
        f"<b>Review Before Saving</b>\n\n"
        f"<b>Firm:</b> {_wizard_data['propfirm_name']}\n"
        f"<b>Profit Target:</b> {_wizard_data['profit_target_pct']:.1f}%\n"
        f"<b>Max DD Overall:</b> {_wizard_data['max_drawdown_overall_pct']:.1f}% → enforced at <b>{eff['max_drawdown_overall_pct']:.1f}%</b> (no buffer — exact)\n"
        f"<b>Max DD Daily:</b> {_wizard_data['max_drawdown_daily_pct']:.1f}% → enforced at <b>{eff['max_drawdown_daily_pct']:.1f}%</b> (−1pp buffer)\n"
        f"<b>Drawdown Type:</b> {'Static' if _wizard_data['drawdown_is_static'] else 'Dynamic'}{dd_flag}\n"
        f"<b>Raw Spread Acct:</b> {'Yes' if _wizard_data['raw_spread_account'] else 'No'}{rs_flag}\n"
        f"<b>Profit Sharing:</b> {_wizard_data['profit_sharing_pct']:.1f}%\n"
        f"<b>Min Profit Days:</b> {_wizard_data['min_profit_days']}\n"
        f"<b>Consistency Threshold:</b> {v:.1f}%\n\n"
        f"<b>Kill conditions:</b>\n"
        f"Kill 1 — daily loss ≥ {eff['max_drawdown_daily_pct']:.1f}% → close all + halt\n"
        f"Kill 2 — overall loss ≥ {eff['max_drawdown_overall_pct']:.1f}% from baseline → close all + <b>permanent halt</b>\n"
        f"Kill 3 — daily profit ≥ {eff['daily_profit_cap_pct']:.1f}% → close all + halt\n"
        f"Kill 4 — overall profit ≥ {_wizard_data['profit_target_pct']:.1f}% → permanent halt\n"
        f"Kill 5 — consistency: largest day &lt; {v:.1f}% of total → permanent halt <i>(Phase 2 only)</i>\n\n"
        f"<i>Baseline equity will be fetched live from MT5 on confirm.</i>\n\n"
        f"Reply <b>YES</b> to save  |  <b>NO</b> to cancel"
    )
    await update.message.reply_text(summary, parse_mode="HTML")
    return PF_CONFIRM


async def _wiz_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip().upper()
    if v == "NO":
        _wizard_data.clear()
        await update.message.reply_text("Cancelled — no changes saved.", parse_mode="HTML")
        return ConversationHandler.END
    if v != "YES":
        await update.message.reply_text(
            "Reply <b>YES</b> to save or <b>NO</b> to cancel.",
            parse_mode="HTML",
        )
        return PF_CONFIRM

    eff = _apply_buffers(_wizard_data)

    # Capture old state before overwriting
    with _pf_lock:
        old_name     = _propfirm.get("propfirm_name", "—")
        old_baseline = _propfirm.get("baseline_equity", 0.0)

    baseline = 0.0
    try:
        baseline = _query_equity(ZMQ_REQ_PROP, "")["balance"]
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Warning</b> — could not fetch live balance:\n<code>{exc}</code>\n\n"
            f"Baseline set to 0.0 — run /changepropfirm again once MT5 is connected.",
            parse_mode="HTML",
        )

    cons_threshold = _wizard_data.get("consistency_threshold_pct", 29.0)
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
            "consistency_threshold_pct":  cons_threshold,
            "baseline_equity":            baseline,
            "day_start_equity":           baseline,
            "day_start_date_utc":         _propfirm_day(_sgt_now()),
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
            "consistency_threshold_pct":  cons_threshold,
        }
        _save_propfirm(_propfirm)

    if baseline > 0:
        _dispatch_parameters()

    # Compute kill dollar levels so user can see them without opening MT5
    floor_amt    = round(baseline * (1.0 - eff["max_drawdown_overall_pct"] / 100.0), 2) if baseline > 0 else 0.0
    daily_dd_amt = round(baseline * eff["max_drawdown_daily_pct"]  / 100.0, 2) if baseline > 0 else 0.0
    cap_amt      = round(baseline * eff["daily_profit_cap_pct"]    / 100.0, 2) if baseline > 0 else 0.0
    target_lvl   = round(baseline * (1.0 + _wizard_data["profit_target_pct"] / 100.0), 2) if baseline > 0 else 0.0
    before_str   = f"{old_name}  |  Baseline: ${old_baseline:,.2f}" if old_name != "—" else "No previous config"

    _wizard_data.clear()
    await update.message.reply_text(
        f"<b>Config Saved</b>\n\n"
        f"Before: {before_str}\n"
        f"After:  <b>{_propfirm['propfirm_name']}</b>  |  Baseline: ${baseline:,.2f}\n\n"
        f"<b>Kill levels (prop account):</b>\n"
        f"Kill 1 daily DD:   −${daily_dd_amt:,.2f} from day-start\n"
        f"Kill 2 overall:    equity ≤ <b>${floor_amt:,.2f}</b>\n"
        f"Kill 3 daily cap:  +${cap_amt:,.2f} from day-start\n"
        f"Kill 4 profit tgt: equity ≥ ${target_lvl:,.2f}\n\n"
        f"Send /phase1 or /phase2 to start trading.",
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
        "<b>Cancelled: /changepropfirm</b>\n\nNo changes saved.\nType /changepropfirm to start again.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ── Telegram commands ─────────────────────────────────────────────────────

async def _cmd_phase1(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return

    with _pf_lock:
        old_baseline = _propfirm.get("baseline_equity", 0.0)

    with _state_lock:
        _phase_state["phase"] = 1
        _phase_state.pop("permanently_halted", None)
        _phase_state.pop("phase1_permanently_halted", None)  # backward compat
        _save_phase(_phase_state)

    balance, err = await asyncio.to_thread(_lock_baseline_from_live)
    if err:
        await update.message.reply_text(
            f"<b>Phase 1 Set</b> — personal lots ×{PHASE_MULT[1]:.2f}\n\n"
            f"<b>Warning</b> — could not fetch live balance:\n<code>{err}</code>\n\n"
            f"Baseline NOT updated. Run /phase1 again once MT5 is connected.",
            parse_mode="HTML",
        )
        logger.warning("Telegram /phase1: baseline lock failed: %s", err)
        return

    await asyncio.to_thread(_dispatch_parameters)

    with _pf_lock:
        pf = dict(_propfirm)
    dd_daily   = pf.get("max_drawdown_daily_pct",  0.0)
    dd_overall = pf.get("max_drawdown_overall_pct", 0.0)
    cap        = pf.get("daily_profit_cap_pct",     0.0)
    target     = pf.get("profit_target_pct",        0.0)
    floor_amt    = round(balance * (1.0 - dd_overall / 100.0), 2) if dd_overall > 0 and balance > 0 else 0.0
    daily_dd_amt = round(balance * dd_daily   / 100.0, 2) if dd_daily  > 0 and balance > 0 else 0.0
    cap_amt      = round(balance * cap        / 100.0, 2) if cap       > 0 and balance > 0 else 0.0
    target_lvl   = round(balance * (1.0 + target / 100.0), 2) if target > 0 and balance > 0 else 0.0

    await update.message.reply_text(
        f"<b>Phase 1 Active</b>\n\n"
        f"Lots multiplier: ×{PHASE_MULT[1]:.2f}\n"
        f"Baseline equity:\n"
        f"  Before: ${old_baseline:,.2f}\n"
        f"  After:  <b>${balance:,.2f}</b> (locked from live MT5)\n\n"
        f"<b>Kill levels (prop account):</b>\n"
        f"Kill 1 daily DD:   −${daily_dd_amt:,.2f} from day-start\n"
        f"Kill 2 overall:    equity ≤ <b>${floor_amt:,.2f}</b>\n"
        f"Kill 3 daily cap:  +${cap_amt:,.2f} from day-start\n"
        f"Kill 4 profit tgt: equity ≥ ${target_lvl:,.2f}\n\n"
        f"Send /resume to start trading.",
        parse_mode="HTML",
    )
    logger.info("Telegram: phase set to 1  baseline=%.2f", balance)


# ── Phase 2 setup wizard (/phase2) ───────────────────────────────────────

async def _cmd_phase2(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END

    with _pf_lock:
        phase1_cfg = _propfirm.get("phase_configs", {}).get("1")

    if not phase1_cfg:
        await update.message.reply_text(
            "<b>Phase 1 config not found.</b>\n\n"
            "Run /changepropfirm first to configure Phase 1 settings.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    _p2_wizard_data.clear()
    _p2_wizard_data["phase1_config"] = dict(phase1_cfg)
    new_cfg = dict(phase1_cfg)
    new_cfg.setdefault("consistency_threshold_pct", 29.0)
    _p2_wizard_data["new_config"] = new_cfg

    block = _p2_settings_block(phase1_cfg)
    await update.message.reply_text(
        f"<b>Phase 2 Setup</b>\n\n"
        f"Phase 1 settings:\n<pre>{block}</pre>\n\n"
        f"Use the same details for Phase 2? (<b>yes</b> / <b>no</b>)",
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
            f"<pre>{block}</pre>\n\n"
            f"Which settings to change? Reply with numbers separated by spaces.\n"
            f"Example: <code>2 4</code> — numbers 1–9",
            parse_mode="HTML",
        )
        return P2_WHICH_FIELDS
    await update.message.reply_text("Reply <b>yes</b> or <b>no</b>.", parse_mode="HTML")
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
            "No valid numbers. Enter numbers 1–9 separated by spaces (e.g. <code>2 4</code>):",
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
        f"<b>Setting {fields[idx]}: {name}</b>\n"
        f"Phase 1 value: {_p2_display(key, current)}\n\n"
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
        f"<b>Phase 2 Review</b>\n\n"
        f"<pre>{block}</pre>\n\n"
        f"<b>Kill conditions:</b>\n"
        f"Kill 1 — daily loss ≥ {eff['max_drawdown_daily_pct']:.1f}%\n"
        f"Kill 2 — overall loss ≥ {eff['max_drawdown_overall_pct']:.1f}%\n"
        f"Kill 3 — daily profit ≥ {eff['daily_profit_cap_pct']:.1f}%\n"
        f"Kill 4 — overall profit ≥ {new['profit_target_pct']:.1f}%\n"
        f"Kill 5 — consistency: largest day &lt; {new.get('consistency_threshold_pct', 29.0):.1f}% of total → permanent halt\n"
        f"Drawdown: {_p2_display('drawdown_is_static', new['drawdown_is_static'])}{dd_flag}\n"
        f"Raw spread: {_p2_display('raw_spread_account', new['raw_spread_account'])}{rs_flag}\n\n"
        f"<i>Baseline equity locked from live MT5 on confirm.</i>\n\n"
        f"Reply <b>YES</b> to save and start Phase 2  |  <b>NO</b> to cancel",
        parse_mode="HTML",
    )
    return P2_CONFIRM


async def _p2_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    v = update.message.text.strip().upper()
    if v == "NO":
        _p2_wizard_data.clear()
        await update.message.reply_text("Cancelled — no changes saved.")
        return ConversationHandler.END
    if v != "YES":
        await update.message.reply_text("Reply <b>YES</b> to save or <b>NO</b> to cancel.", parse_mode="HTML")
        return P2_CONFIRM

    new = _p2_wizard_data["new_config"]
    eff = _apply_buffers(new)

    with _pf_lock:
        old_baseline = _propfirm.get("baseline_equity", 0.0)

    baseline = 0.0
    try:
        baseline = _query_equity(ZMQ_REQ_PROP, "")["balance"]
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Warning</b> — could not fetch live balance:\n<code>{exc}</code>\n\n"
            f"Baseline set to 0.0 — run /phase2 again once MT5 is connected.",
            parse_mode="HTML",
        )

    today = _propfirm_day(_sgt_now())
    cons_threshold = new.get("consistency_threshold_pct", 29.0)
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
            "day_start_equity":           baseline,
            "day_start_date_utc":         today,
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
        f"<b>Phase 2 Active</b>\n\n"
        f"Firm: {_propfirm['propfirm_name']}\n"
        f"Lots multiplier: ×{PHASE_MULT[2]:.2f}\n"
        f"Baseline equity:\n"
        f"  Before: ${old_baseline:,.2f}\n"
        f"  After:  <b>${baseline:,.2f}</b> (locked from live MT5)\n\n"
        f"<b>Kill levels (prop account):</b>\n"
        f"Kill 1 daily DD:   −${daily_dd_amt:,.2f} from day-start\n"
        f"Kill 2 overall:    equity ≤ <b>${floor_amt:,.2f}</b>\n"
        f"Kill 3 daily cap:  +${cap_amt:,.2f} from day-start\n"
        f"Kill 4 profit tgt: equity ≥ ${target_lvl:,.2f}\n\n"
        f"Send /resume to start trading.",
        parse_mode="HTML",
    )
    logger.info("Phase 2 started — firm=%s  baseline=%.2f", _propfirm["propfirm_name"], baseline)
    return ConversationHandler.END


async def _p2_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _p2_wizard_data.clear()
    await update.message.reply_text(
        "<b>Cancelled: /phase2</b>\n\nNo changes saved.\nType /phase2 to start again.",
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
            f"Prop:     Balance ${prop_eq['balance']:,.2f}  |  Equity: ${prop_eq['equity']:,.2f}",
            f"Personal: Balance ${pers_eq['balance']:,.2f}  |  Equity: ${pers_eq['equity']:,.2f}",
        ]
    except Exception:
        eq_lines = ["Could not query equity"]

    with _state_lock:
        _phase_state["active"] = False
        _save_phase(_phase_state)

    lines: list[str] = ["<b>Signal Processing Halted</b>\n", "<b>Open positions at halt:</b>"]
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

    lines.append("\n<b>Live equity:</b>\n" + "\n".join(eq_lines))

    if total_open > 0:
        lines.append(
            f"\n⚠️ {total_open} position(s) still open — will run to SL/TP naturally.\n"
            f"Use /emergency to force-close all immediately."
        )
    lines.append("\nSend /resume to re-enable signal processing.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    logger.warning("Telegram: halted by user")


async def _cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        p_halt = _phase_state.get("permanently_halted", False)
    if p_halt:
        await update.message.reply_text(
            "<b>Blocked</b> — Profit target was reached. Send /phase2 to configure and start the next phase.",
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
            f"Prop:     Balance ${prop_eq['balance']:,.2f}  |  Equity: ${prop_eq['equity']:,.2f}",
            f"Personal: Balance ${pers_eq['balance']:,.2f}  |  Equity: ${pers_eq['equity']:,.2f}",
        ]
    except Exception:
        eq_lines = ["Could not query equity"]

    curfew_note = "\n<i>SGT curfew active — signals from 12:00 SGT.</i>" if _is_sgt_curfew() else ""
    lines: list[str] = [f"<b>Signal Processing Resumed</b>{curfew_note}\n", "<b>Current open positions:</b>"]
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

    lines.append("\n<b>Live equity:</b>\n" + "\n".join(eq_lines))
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
    if last_ts and last_ts != "never":
        try:
            _sgt_off = timedelta(hours=8)
            last_ts_display = (datetime.fromisoformat(last_ts) + _sgt_off).strftime("%Y-%m-%d %H:%M SGT")
        except Exception:
            last_ts_display = last_ts
    else:
        last_ts_display = "never"
    await update.message.reply_text(
        f"<b>System Status</b>\n\n"
        f"<b>Phase:</b> {phase}  (×{mult})\n"
        f"<b>Active:</b> {'YES' if active else 'NO — halted'}\n"
        f"<b>Perm Halt:</b> {'YES — /phase2 required' if p_halt else 'No'}\n"
        f"<b>SGT Curfew:</b> {'YES (dormant)' if curfew else 'No'}\n"
        f"<b>Max open positions:</b> {max_pos}\n"
        f"<b>Firm:</b> {pf_name}\n\n"
        f"<b>Equity</b>\n"
        f"Baseline:         ${baseline:,.2f}\n"
        f"DD floor:         ${floor:,.2f}  (−{dd_overall:.1f}% from baseline)\n"
        f"Day-start:        ${day_start:,.2f}\n"
        f"Daily DD limit:   {dd_daily:.1f}%\n"
        f"Daily profit cap: {cap:.1f}%\n\n"
        f"<b>Last signal:</b> {last_ts_display}",
        parse_mode="HTML",
    )


async def _cmd_propfirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _pf_lock:
        pf = dict(_propfirm)
    await update.message.reply_text(
        f"<b>Prop Firm Config</b>\n\n"
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
    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_text = f"Balance: ${prop['balance']:,.2f}  |  Equity: ${prop['equity']:,.2f}"
    except Exception as exc:
        prop_text = f"OFFLINE — {exc}"
    try:
        pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_text = f"Balance: ${pers['balance']:,.2f}  |  Equity: ${pers['equity']:,.2f}"
    except Exception as exc:
        pers_text = f"OFFLINE — {exc}"
    await update.message.reply_text(
        f"<b>Live Equity Snapshot</b>\n\n"
        f"<b>Prop (VPS #2):</b>\n{prop_text}\n\n"
        f"<b>Personal (VPS #3):</b>\n{pers_text}",
        parse_mode="HTML",
    )


async def _cmd_emergency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END

    await update.message.reply_text("Fetching open positions…")

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

    lines = ["<b>EMERGENCY HALT — Position Summary</b>\n"]

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
                direction = "LONG" if p["type"] == 0 else "SHORT"
                lines.append(
                    f"  {p['symbol']}  {direction}  {p['volume']:.2f} lots"
                    f"  |  P&amp;L: ${p['profit']:+,.2f}"
                )
                total_open += 1
        lines.append("")

    if total_open == 0:
        lines.append("No positions are currently open on either account.")

    lines.append("Reply <code>CONFIRM</code> to force-close all positions and halt signal processing.")
    lines.append("Send /cancel to abort.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return EMERGENCY_CONFIRM


async def _emergency_execute(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    if (update.message.text or "").strip() != "CONFIRM":
        await update.message.reply_text(
            "Type <code>CONFIRM</code> to proceed, or /cancel to abort.",
            parse_mode="HTML",
        )
        return EMERGENCY_CONFIRM
    await asyncio.to_thread(_dispatch_force_close, "emergency_halt", halt=True)
    await asyncio.sleep(2)  # let MT5 execute the close before querying
    try:
        prop_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        pers_eq = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        eq_text = (
            f"Prop:     Balance ${prop_eq['balance']:,.2f}  |  Equity: ${prop_eq['equity']:,.2f}\n"
            f"Personal: Balance ${pers_eq['balance']:,.2f}  |  Equity: ${pers_eq['equity']:,.2f}"
        )
    except Exception:
        eq_text = "Could not query equity"
    await update.message.reply_text(
        f"<b>EMERGENCY HALT EXECUTED</b>\n\n"
        f"All positions force-closed on both MT5 accounts.\n"
        f"Signal processing halted.\n\n"
        f"<b>Equity after close:</b>\n{eq_text}\n\n"
        f"Send /resume to restart trading.",
        parse_mode="HTML",
    )
    logger.warning("Telegram: emergency halt executed by user")
    return ConversationHandler.END


async def _emergency_abort(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "<b>Cancelled: /emergency</b>\n\nNo positions closed.\nType /emergency to try again.",
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

    lines = ["<b>Open Positions</b>\n"]
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
                direction = "LONG" if p["type"] == 0 else "SHORT"
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
        await update.message.reply_text(f"Prop worker offline: {exc}")
        return
    with _pf_lock:
        pf = dict(_propfirm)

    baseline   = pf.get("baseline_equity",        0.0)
    day_start  = pf.get("day_start_equity",        0.0)
    daily_cap  = pf.get("daily_profit_cap_pct",    0.0)
    daily_dd   = pf.get("max_drawdown_daily_pct",  0.0)
    overall_dd = pf.get("max_drawdown_overall_pct",0.0)
    target_pct = pf.get("profit_target_pct",       0.0)
    equity     = prop["equity"]

    daily_pnl   = equity - day_start
    overall_pnl = equity - baseline
    cap_lim     = baseline * daily_cap   / 100
    dd_day_lim  = baseline * daily_dd    / 100
    dd_all_lim  = baseline * overall_dd  / 100
    target_lim  = baseline * target_pct  / 100

    def _pct(val, lim):
        return abs(val) / lim * 100 if lim > 0 else 0.0

    await update.message.reply_text(
        f"<b>P&amp;L Dashboard (Prop Account)</b>\n\n"
        f"Baseline:    ${baseline:,.2f}\n"
        f"Day started: ${day_start:,.2f}\n"
        f"Now:         ${equity:,.2f}\n\n"
        f"<b>Daily P&amp;L:</b>  ${daily_pnl:+,.2f}\n"
        f"  Profit cap  ${cap_lim:,.2f}\n"
        f"  <code>{_pnl_bar(_pct(daily_pnl, cap_lim))}</code>\n"
        f"  DD limit   -${dd_day_lim:,.2f}\n"
        f"  <code>{_pnl_bar(_pct(-daily_pnl, dd_day_lim))}</code>\n\n"
        f"<b>Overall P&amp;L:</b> ${overall_pnl:+,.2f}\n"
        f"  Target      ${target_lim:,.2f}\n"
        f"  <code>{_pnl_bar(_pct(overall_pnl, target_lim))}</code>\n"
        f"  DD limit   -${dd_all_lim:,.2f}\n"
        f"  <code>{_pnl_bar(_pct(-overall_pnl, dd_all_lim))}</code>",
        parse_mode="HTML",
    )


async def _cmd_health(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://127.0.0.1:8000/health")
        l1 = "✅ alive" if resp.status_code == 200 else f"⚠️ HTTP {resp.status_code}"
    except Exception as exc:
        l1 = f"❌ OFFLINE — {exc}"
    try:
        await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_h = "✅ alive"
    except Exception as exc:
        prop_h = f"❌ OFFLINE — {exc}"
    try:
        await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_h = "✅ alive"
    except Exception as exc:
        pers_h = f"❌ OFFLINE — {exc}"

    await update.message.reply_text(
        f"<b>System Health</b>\n\n"
        f"Layer 1 (Gatekeeper):     {l1}\n"
        f"Layer 2 (Logic Core):     ✅ alive\n"
        f"Worker Prop (VPS #2):     {prop_h}\n"
        f"Worker Personal (VPS #3): {pers_h}",
        parse_mode="HTML",
    )


async def _cmd_news(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        events = await asyncio.to_thread(_fetch_ff_events)
    except Exception as exc:
        await update.message.reply_text(f"FF calendar error: {exc}")
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
        await update.message.reply_text("No high-impact events in the next 4 hours for covered pairs.")
        return

    lines = ["<b>Upcoming High-Impact News (next 4h)</b>\n"]
    for t, ccy, title, pairs in relevant:
        sgt_str   = (t + sgt_off).strftime("%H:%M SGT")
        pairs_str = ", ".join(pairs) if pairs else "—"
        lines.append(f"{sgt_str} — {ccy}: {title}\n  Affects: {pairs_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _cmd_blackboard(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    now     = datetime.now(timezone.utc)
    sgt_off = timedelta(hours=8)
    lines   = ["<b>Active Suppression Blackboard</b>\n"]

    with _news_suppressed_lock:
        news_active = {t: e for t, e in _news_suppressed_pairs.items() if e > now}
    with _manual_suppress_lock:
        manual_active = set(_manual_suppressed_pairs)

    all_pairs = set(news_active) | manual_active
    if not all_pairs:
        await update.message.reply_text("No pairs currently suppressed. Blackboard is clear.")
        return

    for ticker in sorted(all_pairs):
        lines.append(f"<b>{ticker}</b>")
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
        await update.message.reply_text("Usage: /closepair EURUSD")
        return ConversationHandler.END
    ticker = text[1].upper()
    if ticker not in ALLOWED_PAIRS:
        await update.message.reply_text(
            f"Unknown pair: {ticker}\nAllowed: {', '.join(sorted(ALLOWED_PAIRS))}"
        )
        return ConversationHandler.END

    ctx.chat_data["closepair_ticker"] = ticker
    await update.message.reply_text(f"Fetching {ticker} positions…")

    broker_symbol = _SYMBOL_MAP.get(ticker, ticker)
    lines = [f"<b>Close Pair — {ticker}</b>\n"]
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
    lines.append(f"Reply <code>CONFIRM</code> to close all {ticker} positions and block new signals.")
    lines.append("Send /cancel to abort.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return CLOSEPAIR_CONFIRM


async def _closepair_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    if (update.message.text or "").strip() != "CONFIRM":
        await update.message.reply_text(
            "Type <code>CONFIRM</code> to proceed, or /cancel to abort.",
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
            f"Prop:     Balance ${prop_eq['balance']:,.2f}  |  Equity: ${prop_eq['equity']:,.2f}\n"
            f"Personal: Balance ${pers_eq['balance']:,.2f}  |  Equity: ${pers_eq['equity']:,.2f}"
        )
    except Exception:
        eq_text = "Could not query equity"
    await update.message.reply_text(
        f"<b>{ticker} closed and blocked.</b>\n\n"
        f"All {ticker} positions closed on both accounts.\n"
        f"New {ticker} signals suppressed until /resumepair {ticker}.\n\n"
        f"<b>Equity after close:</b>\n{eq_text}",
        parse_mode="HTML",
    )
    logger.warning("Manual closepair executed: %s", ticker)
    return ConversationHandler.END


async def _closepair_abort(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    ticker = ctx.chat_data.get("closepair_ticker", "")
    await update.message.reply_text(
        f"<b>Cancelled: /closepair {ticker}</b>\n\nNo positions closed.\n"
        f"Type /closepair {ticker} to try again.",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def _cmd_resumepair(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        await update.message.reply_text("Usage: /resumepair EURUSD")
        return
    ticker = text[1].upper()
    if ticker not in ALLOWED_PAIRS:
        await update.message.reply_text(
            f"Unknown pair: {ticker}\nAllowed: {', '.join(sorted(ALLOWED_PAIRS))}"
        )
        return

    with _manual_suppress_lock:
        _manual_suppressed_pairs.discard(ticker)
    _dispatch_news_clear(ticker)

    await update.message.reply_text(
        f"<b>{ticker} resumed.</b>\n\nNew {ticker} signals will now be accepted.",
        parse_mode="HTML",
    )
    logger.info("Manual resumepair: %s", ticker)


async def _cmd_setmaxpos(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        await update.message.reply_text(
            "Usage: /setmaxpos &lt;number&gt;\nExample: /setmaxpos 2\nRange: 1–10",
            parse_mode="HTML",
        )
        return
    try:
        n = int(text[1])
        assert 1 <= n <= 10
    except Exception:
        await update.message.reply_text("Enter a whole number between 1 and 10.")
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
            f"\n\n<b>Warning:</b> {n} positions × {PROP_RISK_PCT*100:.1f}% = "
            f"<b>{theoretical:.1f}% theoretical max daily loss</b> if all SLs hit simultaneously.\n"
            f"Daily DD limit (before buffer): {dd_daily_raw:.1f}%"
        )

    await update.message.reply_text(
        f"<b>Max open positions:</b>\n"
        f"  Before: {old_max}\n"
        f"  After:  <b>{n}</b>{warning}",
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
        f"<b>Position Limit</b>\n\n"
        f"Max allowed: {limit}\n"
        f"Currently open (prop): {count_str}",
        parse_mode="HTML",
    )


async def _cmd_consistency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        phase = int(_phase_state.get("phase", 1))

    if phase != 2:
        await update.message.reply_text(
            "<b>Consistency Tracker</b>\n\n"
            "Not active — Phase 2 (funded) only.\n"
            "Run /phase2 to start the funded phase.",
            parse_mode="HTML",
        )
        return

    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_equity = prop["equity"]
    except Exception as exc:
        await update.message.reply_text(f"Prop worker offline: {exc}")
        return

    with _pf_lock:
        pf = dict(_propfirm)

    day_start = pf.get("day_start_equity",          0.0)
    baseline  = pf.get("baseline_equity",            0.0)
    threshold = pf.get("consistency_threshold_pct",  0.0) or 29.0
    firm      = pf.get("propfirm_name",              "—")

    with _cons_lock:
        locked_days = list(_consistency_log.get("days", []))

    today_date    = _propfirm_day(_sgt_now())
    today_running = prop_equity - day_start if day_start > 0 else 0.0

    table_str, total, max_day_val, ratio_pct, rule_met = _build_consistency_table(
        locked_days, today_running, today_date, baseline, threshold,
    )

    if rule_met:
        status_line = f"<b>RULE MET ✓ — ready to submit payout claim to {firm}</b>"
    else:
        days_with_profit = len(locked_days) + (1 if today_running > 0 else 0)
        if days_with_profit < 2:
            status_line = "Need at least 2 profitable days to evaluate"
        else:
            status_line = f"Not met yet — largest day at {ratio_pct:.1f}% (need &lt; {threshold:.1f}%)"

    await update.message.reply_text(
        f"<b>Consistency Tracker</b>\n"
        f"Phase 2  ·  {firm}  ·  Threshold: &lt; {threshold:.0f}%\n\n"
        f"<pre>{table_str}</pre>\n\n"
        f"{status_line}",
        parse_mode="HTML",
    )


async def _cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    await update.message.reply_text(
        "<b>TEE Bot — Commands</b>\n\n"

        "<b>Emergency</b>\n"
        "/emergency\n"
        "Force-close ALL positions on both accounts + halt\n\n"

        "<b>Phase &amp; Trading Control</b>\n"
        "/phase1\n"
        "Enter Phase 1 (×0.20 lots) — runs prop firm config wizard\n"
        "/phase2\n"
        "Enter Phase 2 (×0.70 lots) — same config or update settings\n"
        "/resume\n"
        "Resume signal processing after halt\n"
        "/stop\n"
        "Halt new signals (open trades continue to SL/TP)\n\n"

        "<b>Position Limits</b>\n"
        "/setmaxpos 2\n"
        "Set max simultaneous open trades (1–10)\n"
        "/maxpos\n"
        "Show current limit and open count\n\n"

        "<b>Pair Control</b>\n"
        "/closepair EURUSD\n"
        "Close all positions for a pair + block new signals\n"
        "/resumepair EURUSD\n"
        "Unblock a pair and allow new signals\n\n"

        "<b>Status &amp; Monitoring</b>\n"
        "/positions\n"
        "Open positions on both accounts\n"
        "/equity\n"
        "Live balance + equity on both accounts\n"
        "/pnl\n"
        "Today's P&amp;L vs daily cap and DD limits\n"
        "/consistency\n"
        "Consistency rule tracker (Phase 2 only)\n"
        "/health\n"
        "Ping all 4 layers and report live/dead\n"
        "/news\n"
        "Upcoming high-impact events (next 4h)\n"
        "/blackboard\n"
        "Active suppression blackboard (manual + news)\n"
        "/status\n"
        "Live system status and last signal time\n"
        "/propfirm\n"
        "Current prop firm config\n"
        "/changepropfirm\n"
        "Set up or update prop firm (9-step wizard)\n"
        "/cancel\n"
        "Cancel any wizard mid-flow\n\n"

        "<b>Kill Conditions</b> (automatic)\n"
        "Kill 1 — daily loss ≥ DD daily limit → close all + halt\n"
        "Kill 2 — overall loss ≥ DD overall limit → close all + permanent halt\n"
        "Kill 3 — daily profit ≥ cap → close all + halt\n"
        "Kill 4 — overall profit ≥ target → close all + permanent halt → /phase2\n"
        "Kill 5 — consistency rule met → close all + permanent halt → claim payout\n\n"

        "<b>Trading window:</b> 12:00–00:00 SGT, weekdays only\n\n"

        "<b>Startup Sequence</b>\n"
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
            PF_CONSISTENCY:    [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_consistency)],
            PF_CONFIRM:        [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _wiz_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _wiz_cancel)],
        per_chat=True,
    )

    p2_wizard = ConversationHandler(
        entry_points=[CommandHandler("phase2", _cmd_phase2)],
        states={
            P2_SAME_OR_DIFF: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_same_or_diff)],
            P2_WHICH_FIELDS: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_which_fields)],
            P2_COLLECTING:   [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_collect_field)],
            P2_CONFIRM:      [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p2_confirm)],
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

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(wizard)
    tg_app.add_handler(p2_wizard)
    tg_app.add_handler(emergency_wizard)
    tg_app.add_handler(closepair_wizard)
    tg_app.add_handler(CommandHandler("phase1",        _cmd_phase1))
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

    async def _poll():
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(allowed_updates=["message"])
        logger.info("Telegram bot polling (chat_id=%d)", CHAT_ID)
        await asyncio.Event().wait()  # block forever; thread is daemon so exits with process

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_poll())
