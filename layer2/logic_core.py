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
  - Kill 2 (all phases) : overall loss ≥ max_drawdown_overall_pct → FORCE_CLOSE + permanent halt (no buffer — exact user input)
  - Kill 3 (All phases) : daily profit ≥ daily_profit_cap_pct     → FORCE_CLOSE + halt
  - Kill 4 (All phases) : overall profit ≥ profit_target_pct      → FORCE_CLOSE + permanent halt

SGT kill switch (enforced inline in /signal endpoint):
  - Rejects signals 00:00–08:59 SGT and on weekends
  - Dispatches FORCE_CLOSE once per day at the curfew transition (positions only, no halt)

Environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — required
"""

import asyncio
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from layer1.ff_calendar import fetch_events_sync as _fetch_ff_events
from pydantic import BaseModel, field_validator

from layer2.state import (
    _phase_state, _state_lock, _save_phase,
    _propfirm, _pf_lock,
    _consistency_log, _cons_lock,
    _news_suppressed_pairs, _news_suppressed_lock,
    _news_events_lock,
    _manual_suppressed_pairs, _manual_suppress_lock,
    _mismatch_first_seen,
    ALLOWED_PAIRS, _TICKER_CURRENCIES, _SYMBOL_MAP,
    _NEWS_AWARENESS_WINDOW, _NEWS_TRADING_BAN_WINDOW,
    PROP_RISK_PCT, PHASE_MULT,
    ZMQ_PUSH_PROP, ZMQ_PUSH_PERS, ZMQ_REQ_PROP, ZMQ_REQ_PERS,
    _WORKER_DOWN_THRESHOLD,
    _is_sgt_curfew, _sgt_now, _propfirm_day,
    _record_day_profit, _build_consistency_table,
    _invert, _load_consistency_log,
    _trading_window, _window_lock, _apply_next_window,
    _fmt_price,
    _phase1_load, _phase1_active_stage, _phase1_record_stage_day,
)
from layer2.zmq_helpers import (
    _query_equity, _query_positions, _query_deal_pnl, _snapshot_positions_str,
    _dispatch_force_close, _dispatch_close_ticker, _dispatch_news_suppress,
    _dispatch_news_clear, _close_ticker_on_worker,
    _telegram_alert, _alert_sync,
    _update_day_start, _update_pers_day_start, _push_ticket,
    _query_order_status,
)
from layer2 import telegram_handlers
from layer2 import phase1_strategy, phase2_strategy

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

# ── Equity monitor state (reassigned via global — must stay module-local) ─
_last_curfew_close_date: date | None = None
_prop_fail_count:      int  = 0
_pers_fail_count:      int  = 0
_prop_down:            bool = False
_pers_down:            bool = False
_prop_algo_disabled:   bool = False
_pers_algo_disabled:   bool = False

# ── News pre-close dedup (reassigned via global — must stay module-local) ─
# Tracks (ticker, event_time_iso) pairs already acted on — prevents repeat closes.
_news_closed_events: set[tuple[str, str]] = set()

# Symbols Layer 2 just dispatched a news-close for — used to drive the 📰 News Close
# title in Position Closed alerts.  Layer 3 sees these closes as DEAL_REASON_EXPERT
# (bot logic), so we rely on Layer 2's own intent to identify them as news closes.
# Entries auto-expire after 10 minutes (more than enough for the close to register).
_news_close_dispatched: dict[str, datetime] = {}
_NEWS_DISPATCH_TTL_SECS = 600

# ── Signal-blocked alert dedup ─────────────────────────────────────────────
# Prevents flooding Telegram when TradingView fires multiple signals while the
# system is halted or suppressed.  Key: (ticker, reason_tag).  Value: last alert UTC.
_block_alerted: dict[tuple[str, str], datetime] = {}
_BLOCK_COOLDOWN_SECS = 1800   # 30 min — one reminder per ticker per reason per 30 min


def _maybe_block_alert(ticker: str, reason_tag: str) -> bool:
    """Return True (and record timestamp) if we should send a block alert now."""
    key = (ticker, reason_tag)
    now = datetime.now(timezone.utc)
    last = _block_alerted.get(key)
    if last is None or (now - last).total_seconds() >= _BLOCK_COOLDOWN_SECS:
        _block_alerted[key] = now
        return True
    return False


# ── Dynamic session-time helpers (no static "12:00 SGT" anywhere) ─────────
# Auto-resume actually happens at max(prop-firm day rollover @ 11:00 SGT,
# trading-window start). The text shown to the user must reflect the
# live trading_window config so /setwindow takes effect immediately.

_PROPFIRM_DAY_ROLL_MIN = 11 * 60  # 11:00 SGT = FundingPips daily reset


def _effective_window_text() -> tuple[str, str]:
    """Return (current_start, current_end) HH:MM strings from the live window."""
    with _window_lock:
        win = dict(_trading_window["current_window"])
    return win.get("start", "12:00"), win.get("end", "00:00")


def _next_session_resume_text() -> str:
    """Format the next auto-resume time as 'HH:MM SGT'.

    Auto-resume = max(prop-firm day roll @ 11:00 SGT, trading-window start).
    Uses next_window if scheduled (it swaps to current at the day roll).
    """
    with _window_lock:
        nxt  = _trading_window.get("next_window") or _trading_window["current_window"]
        start = nxt.get("start", "12:00")
    h, m = map(int, start.split(":"))
    win_min = h * 60 + m
    resume_min = max(win_min, _PROPFIRM_DAY_ROLL_MIN)
    return f"{resume_min // 60:02d}:{resume_min % 60:02d} SGT"


def _curfew_range_text() -> str:
    """Format the currently-closed range as 'end–start SGT' from the live window."""
    start, end = _effective_window_text()
    return f"{end}–{start} SGT"


def _soft_kill_overridden(now_sgt: datetime) -> bool:
    """True when the user issued /resume earlier today — suppresses re-fires of
    same-day soft kills (K1, K3, Phase 1 stage_reached) until the prop-firm day rolls.
    Permanent kills (K2, K4, K5) are NEVER suppressed.
    """
    with _state_lock:
        override_day = _phase_state.get("soft_kill_override_day", "")
    return bool(override_day) and override_day == _propfirm_day(now_sgt)

# ── Position close tracking (detects TP/SL exits between equity monitor polls) ─
_prev_prop_pos: dict[tuple[str, int], dict] = {}
_prev_pers_pos: dict[tuple[str, int], dict] = {}
_pos_tracking_initialized: bool = False
# Buffer: when one side closes before the other, wait up to 120s before alerting.
# Prevents duplicate split alerts and false orphan force-closes.
_pending_closes: dict[str, dict] = {}  # symbol → {pers_data, prop_data, first_seen}
_CLOSE_WAIT_SECONDS = 120

# ── Known open positions (registered on confirmed signal dispatch) ─────────
# Source of truth for what the bot opened — used to suppress false mismatch alerts
# when one leg of a hedge closes before the other within the grace window.
_known_open_positions: dict[str, dict] = {}  # symbol → {prop_dir, pers_dir}
_known_pos_lock = threading.Lock()


def _handle_mismatch(ticker: str, mismatch_type: str,
                     prop_dir: int | None, pers_dir: int | None) -> None:
    """Close the orphaned position and alert Telegram. Called after 120 s grace period."""
    _dir = {0: "LONG", 1: "SHORT"}
    if mismatch_type == "prop_only":
        _close_ticker_on_worker(ZMQ_PUSH_PROP, ticker, "orphan_mismatch")
        summary = (
            f"Orphan: {_dir.get(prop_dir, '?')} on Prop Hedge "
            f"(no matching Personal Signal position)\n"
            f"Action: Force-closed Prop Hedge"
        )
    elif mismatch_type == "pers_only":
        _close_ticker_on_worker(ZMQ_PUSH_PERS, ticker, "orphan_mismatch")
        summary = (
            f"Orphan: {_dir.get(pers_dir, '?')} on Personal Signal "
            f"(no matching Prop Hedge position)\n"
            f"Action: Force-closed Personal Signal"
        )
    else:  # same_direction
        _close_ticker_on_worker(ZMQ_PUSH_PROP, ticker, "direction_mismatch")
        _close_ticker_on_worker(ZMQ_PUSH_PERS, ticker, "direction_mismatch")
        summary = (
            f"Both accounts hold {_dir.get(prop_dir, '?')} — hedge broken\n"
            f"Action: Force-closed both accounts"
        )
    logger.error("MISMATCH HANDLED: %s  type=%s", ticker, mismatch_type)

    # Re-check positions 5 s after force-close to confirm the orphan is gone
    time.sleep(5)
    try:
        post_prop = _query_positions(ZMQ_REQ_PROP)
        post_pers = _query_positions(ZMQ_REQ_PERS)
        prop_open = any(p["symbol"] == ticker for p in post_prop)
        pers_open = any(p["symbol"] == ticker for p in post_pers)
        prop_str  = f"Still open — {ticker}" if prop_open else "No open positions"
        pers_str  = f"Still open — {ticker}" if pers_open else "No open positions"
        if not prop_open and not pers_open:
            resolution = "✅ Resolved — both accounts are flat."
        else:
            resolution = "⚠️ Action required — check MT5 immediately."
    except Exception as exc:
        logger.warning("Post-mismatch position re-check failed: %s", exc)
        prop_str  = "Query failed"
        pers_str  = "Query failed"
        resolution = "⚠️ Could not verify — check MT5 on both accounts."

    _alert_sync(
        f"⚠️ <b>Mismatch Detected &amp; Resolved — {ticker}</b>\n\n"
        f"{summary}\n\n"
        f"<b>After Close</b>\n"
        f"Personal Signal: {pers_str}\n"
        f"Prop Hedge: {prop_str}\n\n"
        f"{resolution}"
    )


def _run_mismatch_check(prop_positions: list[dict], pers_positions: list[dict]) -> None:
    """Compare open positions on both accounts. Act on any mismatch persisting ≥ 120 s.

    Correct state: every ticker on prop has the OPPOSITE direction on personal.
    Mismatch types:
      prop_only      — ticker open on prop, missing on personal
      pers_only      — ticker open on personal, missing on prop
      same_direction — both accounts have same direction (hedge broken)
    """
    now   = datetime.now(timezone.utc)
    grace = 120  # seconds — must be ≥ _CLOSE_WAIT_SECONDS to avoid false force-closes

    prop_map: dict[str, int] = {p["symbol"]: p["type"] for p in prop_positions}
    pers_map: dict[str, int] = {p["symbol"]: p["type"] for p in pers_positions}
    all_tickers = set(prop_map) | set(pers_map)

    current_mismatches: set[str] = set()

    for ticker in all_tickers:
        mismatch_type: str | None = None
        if ticker in prop_map and ticker not in pers_map:
            mismatch_type = "prop_only"
        elif ticker in pers_map and ticker not in prop_map:
            mismatch_type = "pers_only"
        elif ticker in prop_map and ticker in pers_map:
            if prop_map[ticker] == pers_map[ticker]:
                mismatch_type = "same_direction"

        if not mismatch_type:
            continue  # correct hedge (opposite directions present) — nothing to do

        current_mismatches.add(ticker)
        # If a close is pending for this ticker, the mismatch is expected (one leg
        # closed before the other). Skip — the pending buffer will handle it.
        if ticker in _pending_closes:
            logger.debug("Mismatch check: skipping %s (close pending)", ticker)
            _mismatch_first_seen.pop(ticker, None)
            continue

        if ticker not in _mismatch_first_seen:
            _mismatch_first_seen[ticker] = (now, mismatch_type)
            logger.warning("Mismatch first seen: %s  type=%s", ticker, mismatch_type)
        else:
            first_seen, _ = _mismatch_first_seen[ticker]
            if (now - first_seen).total_seconds() >= grace:
                _handle_mismatch(ticker, mismatch_type,
                                 prop_map.get(ticker), pers_map.get(ticker))
                _mismatch_first_seen.pop(ticker, None)

    # Clear mismatches that resolved themselves within the grace period
    for ticker in list(_mismatch_first_seen):
        if ticker not in current_mismatches:
            logger.info("Mismatch self-resolved for %s (within %ds grace)", ticker, grace)
            del _mismatch_first_seen[ticker]


def _detect_closes(prop_pos: list[dict], pers_pos: list[dict]) -> None:
    """Detect positions closed since the last poll.

    When one side closes before the other (e.g. personal SL hits one poll cycle
    before prop TP), the close is held in _pending_closes for up to
    _CLOSE_WAIT_SECONDS (120 s).  A single combined alert fires only after both
    legs confirm closed, or after the wait window expires.  This prevents the
    duplicate split-messages and false orphan force-closes seen when both legs
    of a hedge close within minutes of each other.
    """
    global _prev_prop_pos, _prev_pers_pos, _pos_tracking_initialized, _pending_closes

    prop_map: dict[tuple[str, int], dict] = {(p["symbol"], p["type"]): p for p in prop_pos}
    pers_map: dict[tuple[str, int], dict] = {(p["symbol"], p["type"]): p for p in pers_pos}

    if not _pos_tracking_initialized:
        _prev_prop_pos = prop_map
        _prev_pers_pos = pers_map
        _pos_tracking_initialized = True
        return

    prop_closed = {k: v for k, v in _prev_prop_pos.items() if k not in prop_map}
    pers_closed = {k: v for k, v in _prev_pers_pos.items() if k not in pers_map}
    _prev_prop_pos = prop_map
    _prev_pers_pos = pers_map

    now = datetime.now(timezone.utc)

    # Merge newly-closed positions into the pending buffer.
    newly_detected = sorted(set(k[0] for k in prop_closed) | set(k[0] for k in pers_closed))
    for symbol in newly_detected:
        prop_key = next((k for k in prop_closed if k[0] == symbol), None)
        pers_key = next((k for k in pers_closed if k[0] == symbol), None)
        prop_data = prop_closed[prop_key] if prop_key else None
        pers_data = pers_closed[pers_key] if pers_key else None

        if symbol in _pending_closes:
            # Fill in whichever side just closed.
            if pers_data and _pending_closes[symbol]["pers_data"] is None:
                _pending_closes[symbol]["pers_data"] = pers_data
            if prop_data and _pending_closes[symbol]["prop_data"] is None:
                _pending_closes[symbol]["prop_data"] = prop_data
        else:
            _pending_closes[symbol] = {
                "pers_data":  pers_data,
                "prop_data":  prop_data,
                "first_seen": now,
            }
            logger.info("Close pending: %s  pers=%s prop=%s",
                        symbol, "yes" if pers_data else "no", "yes" if prop_data else "no")

    # Flush entries where both sides confirmed or the wait window has elapsed.
    for symbol in list(_pending_closes.keys()):
        entry   = _pending_closes[symbol]
        elapsed = (now - entry["first_seen"]).total_seconds()
        both_confirmed = entry["pers_data"] is not None and entry["prop_data"] is not None

        if both_confirmed or elapsed >= _CLOSE_WAIT_SECONDS:
            del _pending_closes[symbol]
            _send_close_alert(symbol, entry["pers_data"], entry["prop_data"])


def _send_close_alert(symbol: str, pers_pos_data: dict | None, prop_pos_data: dict | None) -> None:
    """Build and send the Position Closed Telegram alert for one symbol.

    Layout mirrors the Trade Opened message: one variable per line, section headers
    in bold with direction merged in, four close-type emojis (🟢 TP / 🔴 SL /
    📰 News / ⚠️ Other).  Account mode (demo/real) is read from MT5 directly via
    Layer 3 — no env var, fully automatic.
    """
    # Fresh re-query — gives accurate after-close snapshot.
    try:
        curr_prop = _query_positions(ZMQ_REQ_PROP)
    except Exception:
        curr_prop = []
    try:
        curr_pers = _query_positions(ZMQ_REQ_PERS)
    except Exception:
        curr_pers = []

    def _pos_summary(pos_list: list[dict]) -> str:
        if not pos_list:
            return "  No open positions"
        return "\n".join(
            f"  {p['symbol']} {'↑ LONG' if p['type'] == 0 else '↓ SHORT'} {p['volume']:.2f} lots"
            for p in pos_list
        )

    # Query actual deal P&L from Layer 3 — always returns account_mode even when
    # deal not yet found (MetaQuotes Demo lag).  Returns None only on transport error.
    pers_deal = _query_deal_pnl(ZMQ_REQ_PERS, symbol) if pers_pos_data else None
    prop_deal = _query_deal_pnl(ZMQ_REQ_PROP, symbol) if prop_pos_data else None

    # Account mode — read whichever side we got back; personal is the authoritative one.
    account_mode = "unknown"
    if pers_deal and pers_deal.get("account_mode"):
        account_mode = pers_deal["account_mode"]
    elif prop_deal and prop_deal.get("account_mode"):
        account_mode = prop_deal["account_mode"]

    # Was this a news-triggered close?  (Layer 2 tracks its own dispatches.)
    now_utc = datetime.now(timezone.utc)
    is_news_close = False
    dispatch_ts = _news_close_dispatched.get(symbol)
    if dispatch_ts and (now_utc - dispatch_ts).total_seconds() <= _NEWS_DISPATCH_TTL_SECS:
        is_news_close = True
        _news_close_dispatched.pop(symbol, None)

    # Reason label (per side) — prefer Layer 3 deal.reason, override to NEWS when applicable.
    def _reason_label(deal: dict | None) -> str:
        if is_news_close:
            return "NEWS"
        if deal and deal.get("found") and deal.get("close_reason"):
            return deal["close_reason"]
        # Fallback inference when deal data is unavailable (demo)
        if deal is None:
            return "—"
        return "—"

    pers_reason = _reason_label(pers_deal)
    prop_reason = _reason_label(prop_deal)

    sections: list[str] = []

    # ── Title — driven by close reason if available, else personal P&L sign ──
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
            # Fall back to P&L sign for demo accounts where deal data hasn't arrived
            pers_pnl = pers_pos_data["profit"]
            title = (
                f"🟢 <b>{symbol} — Take Profit</b>" if pers_pnl >= 0
                else f"🔴 <b>{symbol} — Stop Loss</b>"
            )
    else:
        title = f"⚠️ <b>{symbol} — Position Closed</b>"
    sections.append(title)

    # ── Helper to build one side block ───────────────────────────────────────
    def _side_block(label: str, pos_data: dict | None, deal: dict | None, reason_label: str) -> str:
        if not pos_data:
            return f"<b>{label}</b>\nNo matching position — already closed"
        dir_str = "↑ LONG" if pos_data["type"] == 0 else "↓ SHORT"
        if deal and deal.get("found"):
            pnl        = deal["net_pnl"]
            exit_price = _fmt_price(symbol, deal["close_price"])
            commission = deal["commission"]
            pnl_line   = f"P&amp;L: ${pnl:+,.2f}"
            comm_line  = f"Commission: ${commission:+,.2f}"
        else:
            pnl = pos_data["profit"]
            # No deal data → fall back to theoretical exit level
            exit_price = (
                _fmt_price(symbol, pos_data["tp"]) if pnl >= 0
                else _fmt_price(symbol, pos_data["sl"])
            )
            pnl_line  = f"P&amp;L: ${pnl:+,.2f} (est.)"
            comm_line = None
        lines = [
            f"<b>{label} — {dir_str}</b>",
            f"Size: {pos_data['volume']:.2f} lots",
            f"Entry: {_fmt_price(symbol, pos_data['price_open'])}",
            f"Exit: {exit_price}",
            f"Reason: {reason_label}",
            pnl_line,
        ]
        if comm_line:
            lines.append(comm_line)
        if pos_data.get("ticket"):
            lines.append(f"Ticket: {pos_data['ticket']}")
        return "\n".join(lines)

    sections.append(_side_block("Personal Signal", pers_pos_data, pers_deal, pers_reason))
    sections.append(_side_block("Prop Hedge",      prop_pos_data, prop_deal, prop_reason))

    # ── After Close ──────────────────────────────────────────────────────────
    sections.append(
        f"<b>After Close</b>\n"
        f"<b>Personal Signal:</b>\n{_pos_summary(curr_pers)}\n\n"
        f"<b>Prop Hedge:</b>\n{_pos_summary(curr_prop)}"
    )

    # ── Equity ───────────────────────────────────────────────────────────────
    try:
        pers_eq = _query_equity(ZMQ_REQ_PERS, "")["equity"]
        pers_eq_str = f"${pers_eq:,.2f}"
    except Exception:
        pers_eq_str = "OFFLINE"
    try:
        prop_eq = _query_equity(ZMQ_REQ_PROP, "")["equity"]
        prop_eq_str = f"${prop_eq:,.2f}"
    except Exception:
        prop_eq_str = "OFFLINE"
    sections.append(
        f"<b>Equity</b>\n"
        f"Personal Signal: {pers_eq_str}\n"
        f"Prop Hedge: {prop_eq_str}"
    )

    # ── Footer — context line for demo / live data state ─────────────────────
    pers_deal_missing = pers_pos_data and not (pers_deal and pers_deal.get("found"))
    prop_deal_missing = prop_pos_data and not (prop_deal and prop_deal.get("found"))
    any_deal_missing  = pers_deal_missing or prop_deal_missing
    if any_deal_missing:
        if account_mode == "demo":
            sections.append(
                "ℹ️ Demo account — exact MT5 figures will sync to the journal in ~2-3h."
            )
        elif account_mode == "real":
            sections.append(
                "⚠️ Deal data unavailable from broker — check journal dashboard shortly."
            )
        # account_mode == "unknown" or "contest": stay silent (no spurious footer)

    # Clear from known-open-positions tracker.
    with _known_pos_lock:
        _known_open_positions.pop(symbol, None)

    logger.info("Close detection: alert sent for %s", symbol)
    _alert_sync("\n\n".join(sections))


def _run_news_preclose_check() -> None:
    global _news_closed_events

    with _state_lock:
        if int(_phase_state.get("phase", 1)) == 1:
            return  # Phase 1 (evaluation): no prop-firm news rule — skip pre-close

    now            = datetime.now(timezone.utc)
    awareness_td   = timedelta(minutes=_NEWS_AWARENESS_WINDOW)
    ban_td         = timedelta(minutes=_NEWS_TRADING_BAN_WINDOW)

    try:
        events = _fetch_ff_events()
    except Exception as exc:
        logger.warning("News pre-close: FF calendar fetch failed: %s", exc)
        return

    # Expire dedup entries older than 3 hours.
    cutoff = now - timedelta(hours=3)
    with _news_events_lock:
        _news_closed_events = {
            (t, ts) for (t, ts) in _news_closed_events
            if datetime.fromisoformat(ts) > cutoff
        }

    # Expire suppression windows that have ended — alert Telegram, then send NEWS_CLEAR to Layer 3.
    with _news_suppressed_lock:
        expired = [(t, end) for t, end in _news_suppressed_pairs.items() if end <= now]

    if expired:
        _sgt = timedelta(hours=8)
        pair_lines = []
        for t, end in sorted(expired):
            expired_sgt = (end + _sgt).strftime("%H:%M SGT")
            pair_lines.append(f"🔴 → 🟢  <b>{t}</b> — window expired (was until {expired_sgt})")
        _alert_sync(
            f"🟢 <b>News Window Cleared</b>\n\n"
            + "\n".join(pair_lines)
            + "\n\nNew signals accepted for these pairs."
        )

    for t, _ in expired:
        with _news_suppressed_lock:
            _news_suppressed_pairs.pop(t, None)
        _dispatch_news_clear(t)
        logger.info("NEWS suppression window closed for %s", t)

    # ── Two-stage news-first scan ─────────────────────────────────────────
    # Group affected tickers by (currency, event_utc) so we send ONE message per event,
    # not one per ticker. Outer loop: events. Inner loop: pairs affected by that currency.
    #
    # Stage 1 (awareness zone, 31–60 min before): log only, no action.
    # Stage 2 (ban zone, 0–30 min before + 0–30 min after): close + suppress ONCE.

    # event_key → list of newly-triggered tickers for this run
    event_tickers: dict[str, list[str]] = {}
    # event_key → metadata for the alert
    event_meta: dict[str, dict] = {}

    for event in events:
        if event.get("impact") != "High":
            continue

        event_utc = event.get("time_utc")
        if event_utc is None:
            continue

        time_to_event = event_utc - now
        mins_to_event = time_to_event.total_seconds() / 60

        if not (-_NEWS_TRADING_BAN_WINDOW <= mins_to_event <= _NEWS_AWARENESS_WINDOW):
            continue

        for ticker, currencies in _TICKER_CURRENCIES.items():
            if event.get("currency") not in currencies:
                continue

            # Stage 1 — awareness only (31–60 min away): log, no close, no suppress.
            if mins_to_event > _NEWS_TRADING_BAN_WINDOW:
                logger.info(
                    "NEWS AWARENESS %s — [%s] %s @ %s UTC (%.0f min away, watch only)",
                    ticker, event["currency"], event["title"],
                    event_utc.strftime("%Y-%m-%d %H:%M"), mins_to_event,
                )
                continue

            # Stage 2 — ban zone: close + suppress ONCE per (ticker, event_time).
            key = (ticker, event_utc.isoformat())
            with _news_events_lock:
                if key in _news_closed_events:
                    continue
                _news_closed_events.add(key)

            # Suppression ends 30 min after the event.
            suppression_end = event_utc + ban_td
            with _news_suppressed_lock:
                existing = _news_suppressed_pairs.get(ticker)
                if existing is None or suppression_end > existing:
                    _news_suppressed_pairs[ticker] = suppression_end

            _dispatch_news_suppress(ticker, suppression_end)

            # Collect into event group for a single announcement.
            ev_key = f"{event['currency']}|{event_utc.isoformat()}"
            if ev_key not in event_meta:
                event_tickers[ev_key] = []
                event_meta[ev_key] = {
                    "currency":       event["currency"],
                    "title":          event["title"],
                    "event_utc":      event_utc,
                    "suppression_end": suppression_end,
                    "mins_to_event":  mins_to_event,
                }
            event_tickers[ev_key].append(ticker)

    # ── Fire one announcement per event group, then close affected tickers ──
    for ev_key, tickers in event_tickers.items():
        meta      = event_meta[ev_key]
        event_utc = meta["event_utc"]
        sup_end   = meta["suppression_end"]
        mins      = meta["mins_to_event"]
        _sgt      = timedelta(hours=8)

        direction  = f"in {int(mins)} min" if mins >= 0 else f"{int(abs(mins))} min ago"
        event_desc = (
            f"[{meta['currency']}] {meta['title']} "
            f"@ {(event_utc + _sgt).strftime('%H:%M')} SGT ({direction})"
        )

        # Query positions ONCE for this event group — show only positions being closed.
        try:
            pers_pos = _query_positions(ZMQ_REQ_PERS) or []
        except Exception:
            pers_pos = []
        try:
            prop_pos = _query_positions(ZMQ_REQ_PROP) or []
        except Exception:
            prop_pos = []

        tickers_set = set(tickers)
        affected_pers = [p for p in pers_pos if p["symbol"] in tickers_set]
        affected_prop = [p for p in prop_pos if p["symbol"] in tickers_set]

        def _fmt_pos_list(pos_list: list[dict]) -> str:
            return "\n".join(
                f"  {p['symbol']} {'↑ LONG' if p['type'] == 0 else '↓ SHORT'}"
                f" {p['volume']:.2f} lots  P&L: ${p['profit']:+,.2f}"
                for p in pos_list
            ) if pos_list else "  No open positions"

        closing_lines = (
            f"<b>Personal Signal:</b>\n{_fmt_pos_list(affected_pers)}\n\n"
            f"<b>Prop Hedge:</b>\n{_fmt_pos_list(affected_prop)}"
        )

        logger.warning("NEWS BAN [%s] — %s  affected: %s", meta["currency"], event_desc, tickers)
        _alert_sync(
            f"📰 <b>News Pre-Close — [{meta['currency']}]</b>\n\n"
            f"{event_desc}\n\n"
            f"<b>Positions being closed:</b>\n{closing_lines}\n\n"
            f"New signals blocked until "
            f"{(sup_end + _sgt).strftime('%H:%M')} SGT "
            f"(event +{_NEWS_TRADING_BAN_WINDOW} min)."
        )

        for ticker in tickers:
            _dispatch_close_ticker(ticker, f"pre_news_{ticker}")
            _news_close_dispatched[ticker] = datetime.now(timezone.utc)


def _news_preclose_loop() -> None:
    logger.info(
        "News pre-close monitor started (ForexFactory, awareness=%dmin, ban=%dmin)",
        _NEWS_AWARENESS_WINDOW, _NEWS_TRADING_BAN_WINDOW,
    )
    while True:
        time.sleep(60)
        try:
            _run_news_preclose_check()
        except Exception as exc:
            logger.error("News pre-close monitor error: %s", exc)


# ── Equity monitoring ─────────────────────────────────────────────────────

def _equity_monitor_loop() -> None:
    while True:
        time.sleep(30)
        try:
            _run_equity_check()
        except Exception as exc:
            logger.error("Equity monitor error: %s", exc)


def _run_equity_check() -> None:
    global _last_curfew_close_date
    global _prop_fail_count, _pers_fail_count, _prop_down, _pers_down
    global _prop_algo_disabled, _pers_algo_disabled
    global _pos_tracking_initialized

    with _state_lock:
        p_halt = _phase_state.get("permanently_halted", False)
    if p_halt:
        return

    now_sgt  = _sgt_now()
    curfew   = _is_sgt_curfew(now_sgt)
    today    = now_sgt.date()

    if curfew:
        if _last_curfew_close_date != today:
            logger.info("Monitor: SGT curfew transition — dispatching force-close (positions only)")
            pos_str = _snapshot_positions_str()
            _dispatch_force_close("sgt_curfew", halt=False)
            _alert_sync(
                f"🌙 <b>Curfew — All positions closed</b>\n\n"
                f"<b>Positions at close:</b>\n{pos_str}\n\n"
                f"Resumes at next session ({_next_session_resume_text()})."
            )
            _last_curfew_close_date = today
        _pos_tracking_initialized = False
        _pending_closes.clear()  # Discard any stale pending closes across the curfew boundary
        return

    # ── Worker health checks (run every cycle, independent of active state) ──
    try:
        _eq_result  = _query_equity(ZMQ_REQ_PROP, "")
        prop_equity = _eq_result["equity"]
        if _prop_down:
            _prop_down = False
            _prop_fail_count = 0
            _pos_tracking_initialized = False
            _pending_closes.clear()
            _alert_sync("✅ <b>Prop Hedge — Worker Back Online</b>")
        else:
            _prop_fail_count = 0

        # Algo-trading guard: alert once when MT5 disables autotrading, clear when restored
        prop_trade_ok = _eq_result.get("trade_allowed", True)
        if not prop_trade_ok and not _prop_algo_disabled:
            _prop_algo_disabled = True
            _alert_sync(
                "⚠️ <b>Prop Hedge — Algo Trading DISABLED</b>\n\n"
                "MT5 algo trading is off. Orders will be silently rejected.\n\n"
                "<b>Fix</b>\n"
                "1. MT5 toolbar → Algo Trading button (make it green)\n"
                "2. Tools → Options → Expert Advisors → uncheck "
                "<i>'Disable algorithmic trading when the account has been changed'</i>"
            )
        elif prop_trade_ok and _prop_algo_disabled:
            _prop_algo_disabled = False
            _alert_sync("✅ <b>Prop Hedge — Algo Trading Restored</b>")

    except Exception as exc:
        _prop_fail_count += 1
        logger.warning("Monitor: prop equity query failed (%d/%d): %s",
                       _prop_fail_count, _WORKER_DOWN_THRESHOLD, exc)
        if _prop_fail_count >= _WORKER_DOWN_THRESHOLD and not _prop_down:
            _prop_down = True
            _alert_sync(
                "⚠️ <b>Prop Hedge — Worker OFFLINE</b>\n\n"
                f"No response for ~{_WORKER_DOWN_THRESHOLD * 30}s. Positions may still be open.\n\n"
                "<b>Action</b>\n"
                "1. Open VPS #3 noVNC\n"
                "2. <code>cd C:/arbitrage &amp;&amp; uv run python layer3/worker_prop.py</code>"
            )
        return

    pers_equity_live: float | None = None
    try:
        _pers_result = _query_equity(ZMQ_REQ_PERS, "")
        pers_equity_live = _pers_result["equity"]
        if _pers_down:
            _pers_down = False
            _pers_fail_count = 0
            _pos_tracking_initialized = False
            _pending_closes.clear()
            _alert_sync("✅ <b>Personal Signal — Worker Back Online</b>")
        else:
            _pers_fail_count = 0

        pers_trade_ok = _pers_result.get("trade_allowed", True)
        if not pers_trade_ok and not _pers_algo_disabled:
            _pers_algo_disabled = True
            _alert_sync(
                "⚠️ <b>Personal Signal — Algo Trading DISABLED</b>\n\n"
                "MT5 algo trading is off. Orders will be silently rejected.\n\n"
                "<b>Fix</b>\n"
                "1. MT5 toolbar → Algo Trading button (make it green)\n"
                "2. Tools → Options → Expert Advisors → uncheck "
                "<i>'Disable algorithmic trading when the account has been changed'</i>"
            )
        elif pers_trade_ok and _pers_algo_disabled:
            _pers_algo_disabled = False
            _alert_sync("✅ <b>Personal Signal — Algo Trading Restored</b>")

    except Exception as exc:
        _pers_fail_count += 1
        logger.warning("Monitor: personal equity query failed (%d/%d): %s",
                       _pers_fail_count, _WORKER_DOWN_THRESHOLD, exc)
        if _pers_fail_count >= _WORKER_DOWN_THRESHOLD and not _pers_down:
            _pers_down = True
            _alert_sync(
                "⚠️ <b>Personal Signal — Worker OFFLINE</b>\n\n"
                f"No response for ~{_WORKER_DOWN_THRESHOLD * 30}s. Positions may still be open.\n\n"
                "<b>Action</b>\n"
                "1. Open VPS #2 noVNC\n"
                "2. <code>cd C:/arbitrage &amp;&amp; uv run python layer3/worker_personal.py</code>"
            )
        # personal failure doesn't block kill-condition checks — prop equity already fetched

    # Position mismatch + close detection — runs every cycle when both workers are online
    if not _prop_down and not _pers_down:
        try:
            prop_pos = _query_positions(ZMQ_REQ_PROP)
            pers_pos = _query_positions(ZMQ_REQ_PERS)
            _run_mismatch_check(prop_pos, pers_pos)
            _detect_closes(prop_pos, pers_pos)
        except Exception as exc:
            logger.warning("Mismatch check error: %s", exc)

    with _state_lock:
        active     = _phase_state.get("active", False)
        phase      = int(_phase_state.get("phase", 1))
        p_halt     = _phase_state.get("permanently_halted", False)
        d_halt     = _phase_state.get("daily_halted", False)
        d_halt_day = _phase_state.get("daily_halted_date", "")

    # Auto-resume K1/K3 daily halts when a new session begins
    if not active and d_halt and not p_halt and not curfew:
        if d_halt_day != _propfirm_day(now_sgt):
            with _state_lock:
                _phase_state["active"] = True
                _phase_state.pop("daily_halted", None)
                _phase_state.pop("daily_halted_date", None)
                # Stale override from yesterday — clear so today's kills are armed normally
                _phase_state.pop("soft_kill_override_day", None)
                _save_phase(_phase_state)
            active = True
            _alert_sync("🟢 <b>New Session — Auto-Resumed</b>\n\nDaily halt cleared. System is armed and accepting signals.")

    if not active:
        return

    with _pf_lock:
        pf = dict(_propfirm)

    # Reset day-start equity when the prop firm's 11:00 SGT window rolls over
    stored_date = pf.get("day_start_date_utc", "")
    if stored_date != _propfirm_day(now_sgt):
        # Apply scheduled next_window at session rollover
        applied = _apply_next_window()
        if applied:
            logger.info("Trading window applied at session rollover: %s", applied)
        # Lock completed day's profit into consistency log (Phase 2 only)
        if phase == 2 and stored_date:
            day_profit = prop_equity - pf.get("day_start_equity", prop_equity)
            _record_day_profit(stored_date, day_profit)
        _update_day_start(prop_equity)
        if pers_equity_live is not None:
            _update_pers_day_start(pers_equity_live)
        return

    day_start = pf.get("day_start_equity", 0.0)
    baseline  = pf.get("baseline_equity",  0.0)

    if day_start == 0.0:
        _update_day_start(prop_equity)
        if pers_equity_live is not None:
            _update_pers_day_start(pers_equity_live)
        return

    if pf.get("pers_day_start_equity", 0.0) == 0.0 and pers_equity_live is not None:
        _update_pers_day_start(pers_equity_live)

    if baseline <= 0:
        return

    dd_daily_pct   = pf.get("max_drawdown_daily_pct",   0.0)
    dd_overall_pct = pf.get("max_drawdown_overall_pct",  0.0)
    cap_pct        = pf.get("daily_profit_cap_pct",      0.0)

    # K1 daily loss is dynamic: % of that day's starting equity, NOT baseline.
    # Floor resets each session as day_start_equity is updated.
    daily_loss_amt  = round(day_start * dd_daily_pct  / 100.0, 2) if dd_daily_pct  > 0 and day_start > 0 else 0.0
    layer_cap_amt   = round(baseline  * cap_pct        / 100.0, 2) if cap_pct        > 0 else 0.0
    overall_dd_amt  = round(baseline  * dd_overall_pct / 100.0, 2) if dd_overall_pct > 0 else 0.0
    overall_floor   = baseline - overall_dd_amt

    if phase == 1:
        p1 = _phase1_load()
        stages = p1.get("stages", [])
        idx = int(p1.get("active_stage_index", 0))
        decision = phase1_strategy.evaluate_kills(
            prop_equity=prop_equity, baseline=baseline, day_start=day_start,
            dd_daily_pct=dd_daily_pct, dd_overall_pct=dd_overall_pct,
            stages=stages, active_index=idx,
        )
        if decision is None:
            return
        reason     = decision["reason"]
        permanent  = decision["permanent"]
        # User /resume earlier today overrides same-day soft kills.
        # Permanent kills (K2 overall_drawdown_limit, K4 profit_target) still fire.
        if not permanent and _soft_kill_overridden(now_sgt):
            logger.info("Phase1 kill suppressed by /resume override: reason=%s", reason)
            return
        pos_str    = _snapshot_positions_str()
        _dispatch_force_close(reason, halt=True, permanent=permanent)
        if not permanent:
            with _state_lock:
                _phase_state["daily_halted"] = True
                _phase_state["daily_halted_date"] = _propfirm_day(now_sgt)
                _save_phase(_phase_state)
        if reason == "phase1_stage_reached":
            _phase1_record_stage_day(_propfirm_day(now_sgt))
            # advance the ratchet so tomorrow aims at the next stage
            _phase1_active_stage(stages, prop_equity)
            _alert_sync(
                f"🎯 <b>Phase 1 — Stage Reached</b>\n\n"
                f"Prop equity: <b>${prop_equity:,.2f}</b> ≥ stage ${decision['stage_value']:,.2f}\n"
                f"Profitable day locked. Positions force-closed.\n\n"
                f"System auto-resumes at next session ({_next_session_resume_text()}); next target is the following stage."
            )
        elif reason == "daily_loss_limit":
            df = decision["daily_floor"]
            _alert_sync(
                f"🔴 <b>KILL 1 — Daily Loss Limit Hit (Phase 1)</b>\n\n"
                f"Equity: <b>${prop_equity:,.2f}</b>  |  Daily floor: ${df:,.2f}\n"
                f"Day-start: ${day_start:,.2f}\n\n"
                f"All positions force-closed. System auto-resumes at next session ({_next_session_resume_text()}), or /resume to restart now."
            )
        elif reason == "overall_drawdown_limit":
            of = decision["overall_floor"]
            _alert_sync(
                f"🔴 <b>KILL 2 — Overall Drawdown Limit Hit (Phase 1)</b>\n\n"
                f"Equity: <b>${prop_equity:,.2f}</b>  |  Floor: ${of:,.2f}\n\n"
                f"All positions force-closed. Permanent halt.\n"
                f"/changepropfirm → /phase1 → /resume to start a new challenge."
            )
        else:  # profit_target
            _alert_sync(
                f"🏆 <b>KILL 4 — Phase 1 Evaluation PASSED</b>\n\n"
                f"Prop equity: <b>${prop_equity:,.2f}</b> ≥ funded line "
                f"${decision['stage_value']:,.2f}\n\n"
                f"All positions force-closed. System halted.\n\n"
                f"/phase2 to configure and start the funded phase"
            )
        return

    if phase != 1:
        # Kill 2 — static overall floor from baseline (permanent halt)
        if dd_overall_pct > 0 and prop_equity <= overall_floor:
            pos_str = _snapshot_positions_str()
            _dispatch_force_close("overall_drawdown_limit", halt=True, permanent=True)
            msg = (
                f"🔴 <b>KILL 2 — Overall Drawdown Limit Hit</b>\n\n"
                f"Equity: <b>${prop_equity:,.2f}</b>  |  Floor: ${overall_floor:,.2f}\n"
                f"Overall DD: {dd_overall_pct:.1f}%  |  Baseline: ${baseline:,.2f}\n\n"
                f"<b>Positions at close:</b>\n{pos_str}\n\n"
                f"All positions force-closed. Permanent halt.\n\n"
                f"<b>Next steps:</b>\n"
                f"/changepropfirm → /phase1 → /resume to start a new challenge."
            )
            logger.warning("KILL2: equity=%.2f floor=%.2f", prop_equity, overall_floor)
            _alert_sync(msg)
            return

        # Kill 1 — dynamic daily drawdown floor (all phases)
        # Floor = day_start_equity × (1 - dd_daily_pct/100). Resets each session.
        # Dollar limit grows/shrinks with the account — NOT fixed from baseline.
        if daily_loss_amt > 0 and day_start > 0:
            daily_floor = day_start - daily_loss_amt
            if prop_equity <= daily_floor and not _soft_kill_overridden(now_sgt):
                pos_str = _snapshot_positions_str()
                _dispatch_force_close("daily_loss_limit", halt=True)
                with _state_lock:
                    _phase_state["daily_halted"] = True
                    _phase_state["daily_halted_date"] = _propfirm_day(now_sgt)
                    _save_phase(_phase_state)
                msg = (
                    f"🔴 <b>KILL 1 — Daily Loss Limit Hit</b>\n\n"
                    f"Equity: <b>${prop_equity:,.2f}</b>  |  Daily floor: ${daily_floor:,.2f}\n"
                    f"Day-start: ${day_start:,.2f}  |  Max daily loss: ${daily_loss_amt:,.2f} ({dd_daily_pct:.1f}%)\n\n"
                    f"<b>Positions at close:</b>\n{pos_str}\n\n"
                    f"All positions force-closed. System halted for today.\n\n"
                    f"Overall DD floor: ${overall_floor:,.2f}\n\n"
                    f"System auto-resumes at next session ({_next_session_resume_text()}), or /resume to restart now.\n"
                    f"/changepropfirm to switch to a new challenge"
                )
                logger.warning("KILL1: equity=%.2f daily_floor=%.2f day_start=%.2f",
                               prop_equity, daily_floor, day_start)
                _alert_sync(msg)
                return

        # Kill 3 — daily profit cap (all phases)
        # Purpose: protect the consistency rule (no single day > X% of total profit).
        # Level = day_start_equity + (baseline × daily_profit_cap_pct).
        # Resets every session — NOT cumulative across days.
        if layer_cap_amt > 0 and day_start > 0:
            daily_cap_level = day_start + layer_cap_amt
            if prop_equity >= daily_cap_level and not _soft_kill_overridden(now_sgt):
                pos_str = _snapshot_positions_str()
                _dispatch_force_close("daily_profit_cap", halt=True)
                with _state_lock:
                    _phase_state["daily_halted"] = True
                    _phase_state["daily_halted_date"] = _propfirm_day(now_sgt)
                    _save_phase(_phase_state)
                msg = (
                    f"🟡 <b>KILL 3 — Daily Profit Cap Hit</b>\n\n"
                    f"Equity: <b>${prop_equity:,.2f}</b>  |  Cap level: ${daily_cap_level:,.2f}\n"
                    f"Day-start: ${day_start:,.2f}  |  Cap: +${layer_cap_amt:,.2f}\n\n"
                    f"<b>Positions at close:</b>\n{pos_str}\n\n"
                    f"All positions force-closed. System halted for today.\n\n"
                    f"System auto-resumes at next session ({_next_session_resume_text()}), or /resume to restart now."
                )
                logger.warning("KILL3: equity=%.2f cap_level=%.2f day_start=%.2f",
                               prop_equity, daily_cap_level, day_start)
                _alert_sync(msg)
                return

        # Kill 4 — profit target reached (all phases) — cumulative from baseline
        if baseline > 0:
            overall_pct = (prop_equity - baseline) / baseline * 100
            target      = pf.get("profit_target_pct", 0.0)
            if target > 0 and overall_pct >= target:
                pos_str = _snapshot_positions_str()
                _dispatch_force_close("profit_target", halt=True, permanent=True)
                if phase == 1:
                    msg = (
                        f"🏆 <b>KILL 4 — Phase 1 Evaluation PASSED</b>\n\n"
                        f"Profit: <b>{overall_pct:.1f}%</b> ≥ {target:.1f}% target\n"
                        f"Equity: <b>${prop_equity:,.2f}</b>\n\n"
                        f"<b>Positions at close:</b>\n{pos_str}\n\n"
                        f"All positions force-closed. System halted.\n\n"
                        f"/phase2 to configure and start the funded phase\n"
                        f"/changepropfirm to start a new challenge instead"
                    )
                else:
                    msg = (
                        f"🏆 <b>KILL 4 — Phase {phase} Profit Target Reached</b>\n\n"
                        f"Profit: <b>{overall_pct:.1f}%</b> ≥ {target:.1f}% target\n"
                        f"Equity: <b>${prop_equity:,.2f}</b>\n\n"
                        f"<b>Positions at close:</b>\n{pos_str}\n\n"
                        f"All positions force-closed. System halted.\n\n"
                        f"/phase2 to start a new cycle\n"
                        f"/stop to end trading on this account"
                    )
                logger.warning(msg)
                _alert_sync(msg)
                return

        # Kill 5 — Consistency Rule (Phase 2 only)
        # Fires when the largest single profitable day falls below the threshold % of total profit.
        # Includes today's live running P&L so positions are closed the moment the rule is satisfied.
        if phase == 2:
            cons_threshold = pf.get("consistency_threshold_pct", 0.0)
            if cons_threshold > 0:
                with _cons_lock:
                    locked_days = list(_consistency_log.get("days", []))
                today_running = prop_equity - day_start if day_start > 0 else 0.0
                today_date_str = _propfirm_day(now_sgt)

                table_str, total, max_day_val, ratio_pct, rule_met = _build_consistency_table(
                    locked_days, today_running, today_date_str,
                    baseline, cons_threshold,
                )

                if rule_met:
                    overall_pct = total / baseline * 100 if baseline > 0 else 0.0
                    pos_str = _snapshot_positions_str()
                    _dispatch_force_close("consistency_rule", halt=True, permanent=True)
                    msg = (
                        f"🏆 <b>KILL 5 — Consistency Rule Met</b>\n\n"
                        f"<pre>{table_str}</pre>\n\n"
                        f"Overall profit: <b>{overall_pct:.1f}%</b> across {len(locked_days) + (1 if today_running > 0 else 0)} days\n\n"
                        f"<b>Positions at close:</b>\n{pos_str}\n\n"
                        f"All positions force-closed. Trading halted.\n\n"
                        f"Log in to your prop account and submit the profit share withdrawal claim.\n\n"
                        f"/phase2 + /resume to start a new cycle."
                    )
                    logger.warning("KILL 5 — consistency rule met: %.1f%% < %.1f%%", ratio_pct, cons_threshold)
                    _alert_sync(msg)


# ── Module startup ────────────────────────────────────────────────────────

_load_consistency_log()
threading.Thread(target=telegram_handlers._run_bot, daemon=True, name="tg-bot").start()
threading.Thread(target=_equity_monitor_loop,        daemon=True, name="equity-monitor").start()
threading.Thread(target=_news_preclose_loop,          daemon=True, name="news-preclose").start()

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


async def _query_positions_with_retry(zmq_url: str, max_attempts: int = 3) -> tuple[list[dict], str]:
    """Query positions with up to max_attempts retries (3 s apart) to handle transient REP socket timeouts."""
    for attempt in range(max_attempts):
        try:
            positions = await asyncio.to_thread(_query_positions, zmq_url)
            return positions, ""
        except Exception as exc:
            if attempt < max_attempts - 1:
                await asyncio.sleep(3)
            else:
                return [], str(exc)
    return [], "unknown error"


async def _verify_and_notify(
    *,
    ticker: str,
    prop_signal_id: str,
    pers_signal_id: str,
    prop_signal: str,
    prop_lots: float,
    prop_sl: float,
    prop_tp: float,
    prop_dollar_risk: float,
    pers_signal: str,
    pers_lots: float,
    pers_sl: float,
    pers_tp: float,
    pers_dollar_risk: float,
    phase: int,
    baseline_equity: float,
    price_digits: int,
    entry: float,
    sl_distance: float,
    tp_distance: float,
    prop_reward_in: float = 0.0,
    pers_reward_in: float = 0.0,
    prop_rr_in: float = 0.0,
    pers_rr_in: float = 0.0,
) -> None:
    try:
        await _verify_and_notify_inner(
            ticker=ticker, prop_signal_id=prop_signal_id, pers_signal_id=pers_signal_id,
            prop_signal=prop_signal, prop_lots=prop_lots, prop_sl=prop_sl, prop_tp=prop_tp,
            prop_dollar_risk=prop_dollar_risk, pers_signal=pers_signal, pers_lots=pers_lots,
            pers_sl=pers_sl, pers_tp=pers_tp, pers_dollar_risk=pers_dollar_risk,
            phase=phase, baseline_equity=baseline_equity, price_digits=price_digits, entry=entry,
            sl_distance=sl_distance, tp_distance=tp_distance,
            prop_reward_in=prop_reward_in, pers_reward_in=pers_reward_in,
            prop_rr_in=prop_rr_in, pers_rr_in=pers_rr_in,
        )
    except Exception as exc:
        logger.error("_verify_and_notify crashed for %s: %s", ticker, exc, exc_info=True)
        await _telegram_alert(
            f"⚠️ <b>Internal Error — {ticker}</b>\n\n"
            f"Order confirmation task crashed: {exc}\n\n"
            f"Check VPS #1 logs. Positions may be open — verify MT5 manually."
        )


async def _verify_and_notify_inner(
    *,
    ticker: str,
    prop_signal_id: str,
    pers_signal_id: str,
    prop_signal: str,
    prop_lots: float,
    prop_sl: float,
    prop_tp: float,
    prop_dollar_risk: float,
    pers_signal: str,
    pers_lots: float,
    pers_sl: float,
    pers_tp: float,
    pers_dollar_risk: float,
    phase: int,
    baseline_equity: float,
    price_digits: int,
    entry: float,
    sl_distance: float,
    tp_distance: float,
    prop_reward_in: float = 0.0,
    pers_reward_in: float = 0.0,
    prop_rr_in: float = 0.0,
    pers_rr_in: float = 0.0,
) -> None:
    broker_symbol = _SYMBOL_MAP.get(ticker, ticker)
    pers_arrow = "↑ LONG" if pers_signal == "LONG" else "↓ SHORT"
    prop_arrow = "↑ LONG" if prop_signal == "LONG" else "↓ SHORT"

    # Reward = potential profit if TP hits (opposite distance from risk)
    if phase == 1:
        pers_reward, prop_reward = pers_reward_in, prop_reward_in
        pers_rr, prop_rr = pers_rr_in, prop_rr_in
    else:
        pers_reward = round(pers_dollar_risk * (tp_distance / sl_distance), 2) if sl_distance > 0 else 0.0
        prop_reward = round(prop_dollar_risk * (sl_distance / tp_distance), 2) if tp_distance > 0 else 0.0
        pers_rr = tp_distance / sl_distance if sl_distance > 0 else 0.0
        prop_rr = sl_distance / tp_distance if tp_distance > 0 else 0.0

    def _fp(price: float) -> str:
        return _fmt_price(ticker, price)

    def _disc_line(label: str, req: float, actual: float, disc: float) -> str:
        diff_str = f"{disc:.{price_digits}f}"
        if disc == 0.0:
            return f"{label}: {_fp(actual)}"
        return f"{label}: {_fp(actual)} (req {_fp(req)}, diff {diff_str})"

    TERMINAL = {"FILLED", "REJECTED", "CANCELLED", "EXPIRED", "UNSUPPORTED_LIMIT_SETUP", "ERROR"}
    # PENDING_PLACED is a settled outcome too: the market-closed retry window
    # was exhausted and the worker dropped a resting LIMIT order at the signal
    # entry. We stop waiting and report it (it is not a failure).
    SETTLED  = TERMINAL | {"PENDING_PLACED"}

    # Initial wait for Layer 3 to receive and process both tickets
    await asyncio.sleep(5)

    prop_status = await asyncio.to_thread(_query_order_status, ZMQ_REQ_PROP, prop_signal_id)
    pers_status = await asyncio.to_thread(_query_order_status, ZMQ_REQ_PERS, pers_signal_id)
    prop_state  = prop_status.get("status", "UNKNOWN")
    pers_state  = pers_status.get("status", "UNKNOWN")

    # Immediate terminal states (UNSUPPORTED / ERROR / REJECTED before any fill)
    if prop_state in ("UNSUPPORTED_LIMIT_SETUP", "ERROR", "REJECTED") or \
       pers_state in ("UNSUPPORTED_LIMIT_SETUP", "ERROR", "REJECTED"):
        def _side_reason(s: dict, label: str) -> str:
            st  = s.get("status", "UNKNOWN")
            err = s.get("error") or s.get("broker_comment") or ""
            return f"<b>{label}</b>\nStatus: {st}\n{err}" if err else f"<b>{label}</b>\nStatus: {st}"
        await _telegram_alert(
            f"🚫 <b>Signal Not Placed — {ticker}</b>\n\n"
            f"{_side_reason(pers_status, 'Personal Signal')}\n\n"
            f"{_side_reason(prop_status, 'Prop Hedge')}\n\n"
            f"<b>Signal</b>\n"
            f"{pers_arrow} · Entry {_fp(entry)} | SL {_fp(pers_sl)} | TP {_fp(pers_tp)}"
        )
        return

    # Poll until both reach a terminal state — market orders fill in < 1 s
    _prop_final: dict | None = prop_status if prop_state in SETTLED else None
    _pers_final: dict | None = pers_status if pers_state in SETTLED else None

    POLL_INTERVAL = 5    # short: market orders fill almost immediately
    BASE_SECONDS  = 60   # normal wait when no side is mid-retry
    RETRY_GRACE   = 20   # let the worker's final attempt land before we judge

    # A worker may report RETRYING_MARKET_CLOSED — it is re-attempting the
    # market order through the broker's daily settlement break (1-min window).
    # Extend our poll horizon to that worker-provided deadline so we report the
    # eventual fill or the LIMIT fallback, not a premature miss.
    loop_deadline = time.monotonic() + BASE_SECONDS

    async def _refresh(req_addr, signal_id, current):
        nonlocal loop_deadline
        if current is not None:
            return current
        s = await asyncio.to_thread(_query_order_status, req_addr, signal_id)
        st = s.get("status")
        if st in SETTLED:
            return s
        if st == "RETRYING_MARKET_CLOSED":
            rd = s.get("retry_deadline_epoch")
            if rd:
                extended = time.monotonic() + max(0.0, rd - time.time()) + RETRY_GRACE
                loop_deadline = max(loop_deadline, extended)
        return None

    while time.monotonic() < loop_deadline:
        if _prop_final is not None and _pers_final is not None:
            break
        if _is_sgt_curfew():
            break
        with _state_lock:
            if not _phase_state.get("active", True):
                break
        await asyncio.sleep(POLL_INTERVAL)

        _prop_final = await _refresh(ZMQ_REQ_PROP, prop_signal_id, _prop_final)
        _pers_final = await _refresh(ZMQ_REQ_PERS, pers_signal_id, _pers_final)

    # Final settle: a retry / limit fallback may have finalised just as the loop exited.
    if _prop_final is None:
        s = await asyncio.to_thread(_query_order_status, ZMQ_REQ_PROP, prop_signal_id)
        if s.get("status") in SETTLED:
            _prop_final = s
    if _pers_final is None:
        s = await asyncio.to_thread(_query_order_status, ZMQ_REQ_PERS, pers_signal_id)
        if s.get("status") in SETTLED:
            _pers_final = s

    prop_filled = _prop_final is not None and _prop_final.get("status") == "FILLED"
    pers_filled = _pers_final is not None and _pers_final.get("status") == "FILLED"

    if prop_filled and pers_filled:
        # Register as known open position (used by close detector and mismatch checker)
        prop_dir = 0 if prop_signal == "LONG" else 1
        pers_dir = 0 if pers_signal == "LONG" else 1
        with _known_pos_lock:
            _known_open_positions[broker_symbol] = {"prop_dir": prop_dir, "pers_dir": pers_dir}

        pf = _prop_final
        ef = _pers_final

        pers_entry_disc = ef.get("entry_discrepancy", 0.0)
        prop_entry_disc = pf.get("entry_discrepancy", 0.0)

        await _telegram_alert(
            f"<b>{ticker} — Trade Opened</b>\n\n"
            f"<b>Personal Signal — {pers_arrow}</b>\n"
            f"Size: {pers_lots:.2f} lots\n"
            f"Entry: {_fp(ef.get('actual_fill_price', entry))}\n"
            f"SL: {_fp(ef.get('actual_sl', pers_sl))}\n"
            f"TP: {_fp(ef.get('actual_tp', pers_tp))}\n"
            f"Risk: ${pers_dollar_risk:,.2f}\n"
            f"Reward: ${pers_reward:,.2f}\n"
            f"RR: {pers_rr:.2f}\n"
            f"Ticket: {ef.get('mt5_order_ticket', '?')}\n\n"
            f"<b>Prop Hedge — {prop_arrow}</b>\n"
            f"Size: {prop_lots:.2f} lots\n"
            f"Entry: {_fp(pf.get('actual_fill_price', entry))}\n"
            f"SL: {_fp(pf.get('actual_sl', prop_sl))}\n"
            f"TP: {_fp(pf.get('actual_tp', prop_tp))}\n"
            f"Risk: ${prop_dollar_risk:,.2f}\n"
            f"Reward: ${prop_reward:,.2f}\n"
            f"RR: {prop_rr:.2f}\n"
            f"Ticket: {pf.get('mt5_order_ticket', '?')}\n\n"
            f"<b>Context</b>\n"
            f"Phase: Phase {phase}"
            + (f"\nActive stage: ${_phase1_load().get('stages', ['?'])[min(int(_phase1_load().get('active_stage_index',0)), len(_phase1_load().get('stages',[1]))-1)]:,.0f}"
               if phase == 1 else f"\nBaseline: ${baseline_equity:,.2f}")
        )
        return

    # One or both not filled — build appropriate alert
    def _side_summary(s: dict | None, label: str) -> str:
        if s is None:
            return f"<b>{label}</b>\n⚠️ No confirmation received"
        st         = s.get("status", "UNKNOWN")
        ticket_num = s.get("mt5_order_ticket")
        reason     = s.get("broker_comment") or s.get("error") or ""
        if st == "FILLED":
            fill = _fp(s.get("actual_fill_price", entry))
            return (
                f"<b>{label}</b>\n"
                f"✅ Filled @ {fill}"
                f"{f'  |  Ticket: {ticket_num}' if ticket_num else ''}"
            )
        elif st == "PENDING_PLACED":
            px = _fp(s.get("requested_entry", entry))
            return (
                f"<b>{label}</b>\n"
                f"⏳ Limit order resting @ {px} (market was closed; "
                f"fills when price returns)"
                f"{f'  |  Ticket: {ticket_num}' if ticket_num else ''}"
            )
        elif st == "UNSUPPORTED_LIMIT_SETUP":
            return f"<b>{label}</b>\n🚫 {st}\n{reason}"
        else:
            line = f"<b>{label}</b>\n❌ {st}"
            if ticket_num:
                line += f"  |  Ticket: {ticket_num}"
            if reason:
                line += f"\n{reason}"
            return line

    pers_summary = _side_summary(_pers_final, "Personal Signal")
    prop_summary = _side_summary(_prop_final, "Prop Hedge")

    _states  = {(_pers_final or {}).get("status"), (_prop_final or {}).get("status")}
    _hard    = {"REJECTED", "ERROR", "UNSUPPORTED_LIMIT_SETUP", None}
    _resting = "PENDING_PLACED" in _states and not (_states & _hard)
    _header  = (f"⏳ <b>Limit Order Resting — {ticker}</b>" if _resting
                else f"⚠️ <b>Order Not Filled — {ticker}</b>")

    await _telegram_alert(
        f"{_header}\n\n"
        f"{pers_summary}\n\n"
        f"{prop_summary}\n\n"
        f"<b>Signal details</b>\n"
        f"{pers_arrow} · Entry {_fp(entry)} | SL {_fp(pers_sl)} | TP {_fp(pers_tp)}\n"
        f"Lots: Personal {pers_lots:.2f} / Prop {prop_lots:.2f}"
    )


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
        reason  = "weekend" if now_sgt.weekday() >= 5 else f"SGT curfew {_curfew_range_text()}"
        logger.info("GATE %s %s — %s", payload.signal, payload.ticker, reason)
        return JSONResponse({"status": "rejected", "reason": reason})

    with _state_lock:
        active  = _phase_state.get("active", False)
        phase   = int(_phase_state.get("phase", 1))
        p_halt  = _phase_state.get("permanently_halted", False)
        max_pos = _phase_state.get("max_open_positions", 2)

    if p_halt:
        if _maybe_block_alert(payload.ticker, "p_halt"):
            await _telegram_alert(
                f"🔴 <b>Signal Blocked — {payload.ticker}</b>\n\n"
                f"System permanently halted (K2/K4/K5 triggered).\n"
                f"Signal: {payload.signal}\n\n"
                f"Use /phase2 or /changepropfirm then /resume to restart."
            )
        return JSONResponse({
            "status": "halted",
            "reason": "profit target reached — /phase2 to configure and start next phase",
        })

    if not active:
        if _maybe_block_alert(payload.ticker, "halted"):
            await _telegram_alert(
                f"⏸ <b>Signal Skipped — {payload.ticker}</b>\n\n"
                f"System halted (K1/K3 daily halt or manual /stop).\n"
                f"Signal: {payload.signal}\n\n"
                f"Auto-resumes next session, or /resume to restart now."
            )
        logger.info("HALTED — dropped %s %s", payload.signal, payload.ticker)
        return JSONResponse({"status": "halted", "reason": "signal processing stopped"})

    # News + manual suppression gate
    now_utc = datetime.now(timezone.utc)
    with _news_suppressed_lock:
        news_block = (
            phase != 1
            and payload.ticker in _news_suppressed_pairs
            and _news_suppressed_pairs[payload.ticker] > now_utc
        )
    with _manual_suppress_lock:
        manual_block = payload.ticker in _manual_suppressed_pairs
    if news_block or manual_block:
        reason = "manual block (/closepair)" if manual_block else "news suppression window"
        logger.info("SUPPRESSED — dropped %s %s (%s)", payload.signal, payload.ticker, reason)
        if _maybe_block_alert(payload.ticker, reason):
            await _telegram_alert(
                f"📰 <b>Signal Suppressed — {payload.ticker}</b>\n\n"
                f"Reason: {reason}\n"
                f"Signal: {payload.signal}\n\n"
                f"Trading resumes automatically when the window expires."
            )
        return JSONResponse({"status": "suppressed", "reason": reason})

    # Max open positions gate — count by prop positions (1 signal = 1 prop position)
    try:
        open_positions = await asyncio.to_thread(_query_positions, ZMQ_REQ_PROP)
        open_count = len(open_positions)
    except Exception as exc:
        logger.warning("Max-pos check: prop positions query failed: %s — failing open", exc)
        open_count = 0  # fail open — don't block if count unknown

    if open_count >= max_pos:
        logger.info("MAX_POS  %s %s — %d/%d open", payload.signal, payload.ticker, open_count, max_pos)
        await _telegram_alert(
            f"🚫 <b>Signal Skipped — {payload.ticker}</b>\n\n"
            f"Max open positions reached ({open_count}/{max_pos}).\n"
            f"Signal: {payload.signal}\n\n"
            f"/setmaxpos N to increase the limit."
        )
        return JSONResponse({
            "status": "rejected",
            "reason": "max_positions_reached",
            "open":   open_count,
            "max":    max_pos,
        })

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

    # Gate: reject immediately if MT5 algo trading is disabled on either worker.
    # This prevents a silent EXECUTION FAILURE caused by Layer 3 rejecting the order.
    if not prop_info.get("trade_allowed", True):
        msg = (
            f"🚫 <b>Signal Blocked — {payload.ticker}</b>\n\n"
            f"Prop Hedge algo trading is <b>DISABLED</b>.\n\n"
            f"<b>Fix</b>\n"
            f"1. MT5 toolbar → Algo Trading button (make it green)\n"
            f"2. Tools → Options → Expert Advisors → uncheck "
            f"<i>'Disable algorithmic trading when the account has been changed'</i>"
        )
        logger.error("Prop trade_allowed=False — blocking %s %s", payload.signal, payload.ticker)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail="prop trade_allowed=False")

    if not pers_info.get("trade_allowed", True):
        msg = (
            f"🚫 <b>Signal Blocked — {payload.ticker}</b>\n\n"
            f"Personal Signal algo trading is <b>DISABLED</b>.\n\n"
            f"<b>Fix</b>\n"
            f"1. MT5 toolbar → Algo Trading button (make it green)\n"
            f"2. Tools → Options → Expert Advisors → uncheck "
            f"<i>'Disable algorithmic trading when the account has been changed'</i>"
        )
        logger.error("Personal trade_allowed=False — blocking %s %s", payload.signal, payload.ticker)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail="personal trade_allowed=False")

    # Step A — prop dollar risk: strictly 0.67% of static baseline (never live equity)
    with _pf_lock:
        baseline_equity = _propfirm.get("baseline_equity", 0.0)
    if baseline_equity <= 0:
        msg = "baseline_equity not set — send /phase1 or /phase2 via Telegram first"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    prop_dollar_risk = baseline_equity * PROP_RISK_PCT
    phase_ratio      = PHASE_MULT.get(phase, PHASE_MULT[1])

    # Personal account SL distance (signal perspective — used for lot sizing of funded account)
    # Funded account SL = signal TP, so funded SL distance = tp_distance
    sl_distance = abs(payload.entry - payload.sl)   # personal SL distance (signal perspective)
    tp_distance = abs(payload.tp   - payload.entry) # funded SL distance = signal TP distance

    prop_tick_size = prop_info["trade_tick_size"]
    prop_tick_val  = prop_info["trade_tick_value"]

    if prop_tick_size <= 0 or prop_tick_val <= 0:
        msg = (f"Invalid contract data from prop worker for {payload.ticker} — "
               f"tick_size={prop_tick_size} tick_value={prop_tick_val}")
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    if tp_distance <= 0:
        msg = f"TP distance is zero for {payload.ticker} — tp={payload.tp} entry={payload.entry}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=422, detail=msg)

    price_digits = prop_info["digits"]
    prop_contract_size = prop_info.get("contract_size", 0.0)
    pers_contract_size = pers_info.get("contract_size", prop_contract_size)
    pers_tick_size     = pers_info.get("trade_tick_size", prop_tick_size)
    pers_tick_val      = pers_info.get("trade_tick_value", prop_tick_val)

    if phase == 1:
        p1 = _phase1_load()
        stages = p1.get("stages", [])
        if not stages:
            msg = "Phase 1 not configured — run /phase1 to set reward:risk first"
            logger.error(msg)
            await _telegram_alert(f"🚫 <b>Signal Blocked — {payload.ticker}</b>\n\n{msg}")
            raise HTTPException(status_code=503, detail=msg)
        try:
            live_prop_equity = float(prop_info.get("equity", 0.0))
        except Exception:
            live_prop_equity = 0.0
        if live_prop_equity <= 0:
            msg = "Phase 1: live prop equity unavailable — cannot size dynamic reward"
            logger.error(msg)
            await _telegram_alert(f"🚫 <b>Signal Blocked — {payload.ticker}</b>\n\n{msg}")
            raise HTTPException(status_code=503, detail=msg)
        idx = _phase1_active_stage(stages, live_prop_equity)
        if idx >= len(stages):
            msg = "Phase 1: final stage already reached — awaiting K4 / /phase2"
            logger.info(msg)
            return JSONResponse({"status": "halted", "reason": msg})
        g = phase1_strategy.compute_geometry(
            ticker=payload.ticker, signal=payload.signal,
            entry=payload.entry, signal_sl=payload.sl,
            price_digits=price_digits,
            prop_contract_size=prop_contract_size,
            prop_tick_size=prop_tick_size, prop_tick_value=prop_tick_val,
            pers_contract_size=pers_contract_size,
            pers_tick_size=pers_tick_size, pers_tick_value=pers_tick_val,
            active_stage=stages[idx], live_prop_equity=live_prop_equity,
            fixed_risk=float(p1.get("fixed_risk", 0.0)),
            pers_ratio=PHASE_MULT.get(1, 0.20),
            max_prop_lots=float(p1.get("max_prop_lots", 0.0)),
        )
    else:
        g = phase2_strategy.compute_geometry(
            ticker=payload.ticker, signal=payload.signal,
            entry=payload.entry, signal_sl=payload.sl, signal_tp=payload.tp,
            price_digits=price_digits,
            prop_contract_size=prop_contract_size,
            prop_tick_size=prop_tick_size, prop_tick_value=prop_tick_val,
            pers_contract_size=pers_contract_size,
            pers_tick_size=pers_tick_size, pers_tick_value=pers_tick_val,
            baseline_equity=baseline_equity,
            prop_risk_pct=PROP_RISK_PCT, phase_ratio=PHASE_MULT.get(phase, PHASE_MULT[1]),
        )

    if "reject" in g:
        logger.info("GEOMETRY REJECT %s: %s", payload.ticker, g["reject"])
        await _telegram_alert(
            f"🚫 <b>Signal Skipped — {payload.ticker}</b>\n\n"
            f"Phase {phase} sizing rejected: {g['reject']}\n"
            f"Signal: {payload.signal}"
        )
        return JSONResponse({"status": "rejected", "reason": g["reject"]})

    prop_lots        = g["prop_lots"]
    pers_lots        = g["pers_lots"]
    prop_sl          = g["prop_sl"]
    prop_tp          = g["prop_tp"]
    pers_sl          = g["pers_sl"]
    pers_tp          = g["pers_tp"]
    prop_dollar_risk = g["prop_dollar_risk"]
    pers_dollar_risk = g["pers_dollar_risk"]
    sl_distance      = g["sl_distance"]
    tp_distance      = g["tp_distance"]
    prop_signal      = g["prop_signal"]
    pers_signal      = g["pers_signal"]

    logger.info(
        "GEOMETRY phase=%d  prop=%.2f lots ($%.2f risk)  pers=%.2f lots ($%.2f risk)  "
        "prop_sl=%s prop_tp=%s pers_sl=%s pers_tp=%s",
        phase, prop_lots, prop_dollar_risk, pers_lots, pers_dollar_risk,
        prop_sl, prop_tp, pers_sl, pers_tp,
    )

    # Personal follows signal direction; prop is inverse (already resolved in g)
    _base_id = f"{payload.ticker}_{payload.timestamp_ms}"
    prop_ticket = {
        "signal_id":    f"{_base_id}_prop",
        "ticker":       payload.ticker,
        "timestamp_ms": payload.timestamp_ms,
        "entry":        payload.entry,
        "sl":           prop_sl,
        "tp":           prop_tp,
        "sl_pips":      payload.sl_pips,
        "signal":       prop_signal,
        "lots":         prop_lots,
    }
    pers_ticket = {
        "signal_id":    f"{_base_id}_pers",
        "ticker":       payload.ticker,
        "timestamp_ms": payload.timestamp_ms,
        "entry":        payload.entry,
        "sl":           pers_sl,
        "tp":           pers_tp,
        "sl_pips":      payload.sl_pips,
        "signal":       pers_signal,
        "lots":         pers_lots,
    }

    # Both tickets sent as market orders simultaneously
    prop_ticket["order_type"] = "market"
    pers_ticket["order_type"] = "market"

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

    asyncio.create_task(_verify_and_notify(
        ticker=payload.ticker,
        prop_signal_id=prop_ticket["signal_id"],
        pers_signal_id=pers_ticket["signal_id"],
        prop_signal=prop_ticket["signal"],
        prop_lots=prop_lots,
        prop_sl=prop_sl,
        prop_tp=prop_tp,
        prop_dollar_risk=prop_dollar_risk,
        pers_signal=pers_ticket["signal"],
        pers_lots=pers_lots,
        pers_sl=pers_sl,
        pers_tp=pers_tp,
        pers_dollar_risk=pers_dollar_risk,
        phase=phase,
        baseline_equity=baseline_equity,
        price_digits=price_digits,
        entry=payload.entry,
        sl_distance=sl_distance,
        tp_distance=tp_distance,
        prop_reward_in=g.get("prop_reward", 0.0),
        pers_reward_in=g.get("pers_reward", 0.0),
        prop_rr_in=g.get("prop_rr", 0.0),
        pers_rr_in=g.get("pers_rr", 0.0),
    ))

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


@app.get("/news_status")
async def news_status():
    """Returns which pairs are currently in a news suppression window."""
    now = datetime.now(timezone.utc)
    with _news_suppressed_lock:
        active = {
            t: {
                "suppression_ends_utc": end.isoformat(),
                "minutes_remaining":    max(0, int((end - now).total_seconds() / 60)),
            }
            for t, end in _news_suppressed_pairs.items()
            if end > now
        }
    return {
        "suppressed_pairs": active,
        "count":            len(active),
        "utc_time":         now.isoformat(),
    }
