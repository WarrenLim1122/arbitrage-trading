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
from layer1.ff_calendar import fetch_events_sync as _fetch_ff_events
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
ROOT                   = Path(__file__).parent.parent
PHASE_CONFIG_PATH      = ROOT / "config" / "phase_config.json"
RISK_PARAMS_PATH       = ROOT / "config" / "risk_params.json"
PROPFIRM_CONFIG_PATH   = ROOT / "config" / "propfirm_config.json"
CONSISTENCY_LOG_PATH   = ROOT / "config" / "consistency_log.json"

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
    "EURUSD", "GBPUSD", "USDCHF", "USDCAD", "USDJPY",
    "NZDUSD", "XAUUSD", "XAGUSD", "NAS100",
})

# ForexFactory currency codes that each pair is sensitive to.
# FF tags events with currency codes directly (e.g. "USD", "EUR") — no country mapping needed.
_TICKER_CURRENCIES: dict[str, frozenset[str]] = {
    "EURUSD": frozenset({"EUR", "USD"}),
    "GBPUSD": frozenset({"GBP", "USD"}),
    "USDCHF": frozenset({"USD", "CHF"}),
    "USDCAD": frozenset({"USD", "CAD"}),
    "USDJPY": frozenset({"USD", "JPY"}),
    "NZDUSD": frozenset({"NZD", "USD"}),
    "XAUUSD": frozenset({"USD"}),
    "XAGUSD": frozenset({"USD"}),
    "NAS100": frozenset({"USD"}),
}

_NEWS_AWARENESS_WINDOW   = 60   # minutes before news → scan / log only
_NEWS_TRADING_BAN_WINDOW = 30   # minutes before news → close positions + suppress


# ── Shared state ──────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_pf_lock    = threading.Lock()

# ── News pre-close state ───────────────────────────────────────────────────
# Tracks (ticker, event_time_iso) pairs already acted on — prevents repeat closes.
# event_time_iso is event["time_utc"].isoformat() from ff_calendar.
_news_closed_events: set[tuple[str, str]] = set()
_news_events_lock = threading.Lock()

# Active news suppression window per pair.
# ticker → suppression_end (UTC datetime): event_utc + 30 min (post-event ban end).
# Layer 3 workers are notified via ZMQ so they can independently refuse execution.
_news_suppressed_pairs: dict[str, datetime] = {}
_news_suppressed_lock = threading.Lock()

# Manual suppression via /closepair command — persists until /resumepair.
_manual_suppressed_pairs: set[str] = set()
_manual_suppress_lock = threading.Lock()

# Position mismatch tracking: ticker → (first_seen_utc, mismatch_type)
# Populated by equity monitor; no lock needed (single monitor thread).
_mismatch_first_seen: dict[str, tuple[datetime, str]] = {}


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


# ── Consistency log ───────────────────────────────────────────────────────
# Tracks per-day profits for Phase 2. Each entry: {date, profit_usd}.
# Only positive-profit days are stored. Reset at the start of each Phase 2.

_consistency_log: dict = {"days": []}
_cons_lock = threading.Lock()


def _load_consistency_log() -> None:
    global _consistency_log
    if CONSISTENCY_LOG_PATH.exists():
        with CONSISTENCY_LOG_PATH.open() as f:
            _consistency_log = json.load(f)
    else:
        _consistency_log = {"days": []}


def _save_consistency_log() -> None:
    CONSISTENCY_LOG_PATH.parent.mkdir(exist_ok=True)
    with CONSISTENCY_LOG_PATH.open("w") as f:
        json.dump(_consistency_log, f, indent=2)


def _reset_consistency_log() -> None:
    with _cons_lock:
        _consistency_log["days"] = []
        _save_consistency_log()
    logger.info("Consistency log reset for new Phase 2 cycle")


def _record_day_profit(date_str: str, profit_usd: float) -> None:
    """Append a completed trading day's profit. Skips duplicates and non-positive values."""
    if profit_usd <= 0:
        return
    with _cons_lock:
        days = _consistency_log.setdefault("days", [])
        if any(d["date"] == date_str for d in days):
            return
        days.append({"date": date_str, "profit_usd": round(profit_usd, 2)})
        _save_consistency_log()
    logger.info("Consistency: recorded day %s  profit=%.2f", date_str, profit_usd)


def _build_consistency_table(
    locked_days: list[dict],
    today_profit: float,
    today_date: str,
    baseline: float,
    threshold: float,
) -> tuple[str, float, float, float, bool]:
    """Format the consistency table for Telegram.

    Returns (table_str, total, max_day_val, ratio_pct, rule_met).
    rule_met is True only when len >= 2 AND ratio_pct < threshold.
    """
    all_days: list[dict] = [
        {"date": d["date"], "profit_usd": d["profit_usd"], "live": False}
        for d in locked_days if d["profit_usd"] > 0
    ]
    if today_profit > 0:
        all_days.append({"date": today_date, "profit_usd": today_profit, "live": True})

    if not all_days:
        return "No profitable days recorded yet.", 0.0, 0.0, 100.0, False

    total       = sum(d["profit_usd"] for d in all_days)
    max_day_val = max(d["profit_usd"] for d in all_days)
    ratio_pct   = max_day_val / total * 100 if total > 0 else 100.0
    rule_met    = len(all_days) >= 2 and ratio_pct < threshold

    header = f"{'':1}{'Day':<5} {'Date':<10} {'Profit ($)':>12}  {'%Base':>6}  {'%Total':>7}"
    sep    = "─" * 52
    rows   = [header, sep]

    for i, d in enumerate(all_days, 1):
        p         = d["profit_usd"]
        pct_base  = p / baseline * 100 if baseline > 0 else 0.0
        pct_total = p / total * 100
        is_max    = abs(p - max_day_val) < 0.01
        flag      = "★" if is_max else " "
        live_tag  = "~" if d["live"] else " "
        try:
            date_str = datetime.fromisoformat(d["date"]).strftime("%b %d")
        except Exception:
            date_str = d["date"][:6]
        rows.append(
            f"{flag}D{i:<4} {date_str:<10} ${p:>10,.2f}  "
            f"{pct_base:>+5.2f}%  {pct_total:>6.1f}%{live_tag}"
        )

    overall_pct = total / baseline * 100 if baseline > 0 else 0.0
    rows.append(sep)
    rows.append(f" {'Total':<14} ${total:>10,.2f}  {overall_pct:>+5.2f}%")
    rows.append(f" Largest day : {ratio_pct:.1f}%   Threshold: < {threshold:.1f}%")
    rows.append(f" Status      : {'RULE MET ✓' if rule_met else 'not met yet'}")

    legend = []
    if any(d["live"] for d in all_days):
        legend.append("~ today's live P&L (unrealised included)")
    if any(abs(d["profit_usd"] - max_day_val) < 0.01 for d in all_days):
        legend.append("★ largest day")
    if legend:
        rows.append("")
        rows.extend(f" {ln}" for ln in legend)

    return "\n".join(rows), total, max_day_val, ratio_pct, rule_met


_phase_state: dict = _load_phase()
_propfirm:    dict = _load_propfirm()

# Migrate old key name on first load
if "phase1_permanently_halted" in _phase_state and "permanently_halted" not in _phase_state:
    _phase_state["permanently_halted"] = _phase_state.pop("phase1_permanently_halted")
    _save_phase(_phase_state)

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

    - Daily DD: subtract 1 percentage point (buffer against prop firm's daily limit).
    - Overall DD: NO buffer — trigger at exact value user inputs (prop firm closes at this exact %).
    - Daily profit cap: enforce at 25% of target (vs the 30% consistency rule).
    """
    effective = raw.copy()
    effective["max_drawdown_daily_pct"]   = round(raw["max_drawdown_daily_pct"]   - 1.0, 2)
    effective["max_drawdown_overall_pct"] = raw["max_drawdown_overall_pct"]
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
    return now.hour < 12 or now.weekday() >= 5


# ── Telegram alert (sync — safe to call from any thread) ──────────────────

def _alert_sync(message: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        with httpx.Client(timeout=5.0) as client:
            client.post(url, json={
                "chat_id": CHAT_ID,
                "text": f"<b>TEE Alert</b>\n\n{message}",
                "parse_mode": "HTML",
            })
    except Exception as exc:
        logger.error("Telegram sync alert failed: %s", exc)


async def _telegram_alert(message: str) -> None:
    try:
        bot = Bot(token=BOT_TOKEN)
        async with bot:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"<b>TEE Alert</b>\n\n{message}",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.error("Telegram async alert failed: %s", exc)


# ── Force-close dispatch ──────────────────────────────────────────────────

def _dispatch_force_close(reason: str, *, halt: bool = True, permanent: bool = False) -> None:
    """Push FORCE_CLOSE to both Layer 3 workers.

    halt=True     — also sets active=False in phase config (for kill conditions).
    halt=False    — positions closed only; active flag untouched (for SGT curfew).
    permanent=True — sets permanently_halted (profit target reached in any phase).
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
                _phase_state["permanently_halted"] = True
            _save_phase(_phase_state)

    logger.warning("FORCE_CLOSE dispatched — reason=%s  halt=%s  permanent=%s",
                   reason, halt, permanent)


# ── News pre-close helpers ────────────────────────────────────────────────

def _dispatch_close_ticker(ticker: str, reason: str) -> None:
    """Push CLOSE_TICKER to both Layer 3 workers for a single currency pair."""
    ticket = {"action": "CLOSE_TICKER", "ticker": ticker, "reason": reason}
    for url in (ZMQ_PUSH_PROP, ZMQ_PUSH_PERS):
        try:
            _push_ticket(url, ticket)
        except Exception as exc:
            logger.error("CLOSE_TICKER dispatch failed → %s: %s", url, exc)
    logger.warning("CLOSE_TICKER dispatched — ticker=%s  reason=%s", ticker, reason)


def _dispatch_news_suppress(ticker: str, suppression_end: datetime) -> None:
    """Tell both Layer 3 workers to refuse new execution tickets for this pair."""
    ticket = {
        "action":               "NEWS_SUPPRESS",
        "ticker":               ticker,
        "suppression_end_utc":  suppression_end.isoformat(),
    }
    for url in (ZMQ_PUSH_PROP, ZMQ_PUSH_PERS):
        try:
            _push_ticket(url, ticket)
        except Exception as exc:
            logger.error("NEWS_SUPPRESS dispatch failed → %s: %s", url, exc)
    logger.info("NEWS_SUPPRESS dispatched — ticker=%s  until=%s", ticker, suppression_end.isoformat())


def _dispatch_news_clear(ticker: str) -> None:
    """Tell both Layer 3 workers that the news window for this pair has closed."""
    ticket = {"action": "NEWS_CLEAR", "ticker": ticker}
    for url in (ZMQ_PUSH_PROP, ZMQ_PUSH_PERS):
        try:
            _push_ticket(url, ticket)
        except Exception as exc:
            logger.error("NEWS_CLEAR dispatch failed → %s: %s", url, exc)
    logger.info("NEWS_CLEAR dispatched — ticker=%s", ticker)


# ── Position mismatch detection ───────────────────────────────────────────

def _close_ticker_on_worker(zmq_url: str, ticker: str, reason: str) -> None:
    """Close all positions for one ticker on a single worker (not both)."""
    ticket = {"action": "CLOSE_TICKER", "ticker": ticker, "reason": reason}
    try:
        _push_ticket(zmq_url, ticket)
    except Exception as exc:
        logger.error("CLOSE_TICKER (single) failed → %s for %s: %s", zmq_url, ticker, exc)


def _handle_mismatch(ticker: str, mismatch_type: str,
                     prop_dir: int | None, pers_dir: int | None) -> None:
    """Close the orphaned position and alert Telegram. Called after 30 s grace period."""
    _dir = {0: "LONG", 1: "SHORT"}
    if mismatch_type == "prop_only":
        _close_ticker_on_worker(ZMQ_PUSH_PROP, ticker, "orphan_mismatch")
        msg = (
            f"<b>CRITICAL MISMATCH — {ticker}</b>\n\n"
            f"Prop has {_dir.get(prop_dir, '?')} but personal has NONE.\n"
            f"Orphaned prop position force-closed.\n\n"
            f"Check VPS #2 + VPS #3 immediately."
        )
    elif mismatch_type == "pers_only":
        _close_ticker_on_worker(ZMQ_PUSH_PERS, ticker, "orphan_mismatch")
        msg = (
            f"<b>CRITICAL MISMATCH — {ticker}</b>\n\n"
            f"Personal has {_dir.get(pers_dir, '?')} but prop has NONE.\n"
            f"Orphaned personal position force-closed.\n\n"
            f"Check VPS #2 + VPS #3 immediately."
        )
    else:  # same_direction
        _close_ticker_on_worker(ZMQ_PUSH_PROP, ticker, "direction_mismatch")
        _close_ticker_on_worker(ZMQ_PUSH_PERS, ticker, "direction_mismatch")
        msg = (
            f"<b>CRITICAL DIRECTION MISMATCH — {ticker}</b>\n\n"
            f"Both accounts hold {_dir.get(prop_dir, '?')} — hedge is BROKEN!\n"
            f"Positions closed on BOTH accounts.\n\n"
            f"Check VPS #2 + VPS #3 immediately."
        )
    logger.error("MISMATCH HANDLED: %s  type=%s", ticker, mismatch_type)
    _alert_sync(msg)


def _run_mismatch_check(prop_positions: list[dict], pers_positions: list[dict]) -> None:
    """Compare open positions on both accounts. Act on any mismatch persisting ≥ 30 s.

    Correct state: every ticker on prop has the OPPOSITE direction on personal.
    Mismatch types:
      prop_only      — ticker open on prop, missing on personal
      pers_only      — ticker open on personal, missing on prop
      same_direction — both accounts have same direction (hedge broken)
    """
    now   = datetime.now(timezone.utc)
    grace = 30  # seconds

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


def _run_news_preclose_check() -> None:
    global _news_closed_events

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

    # Expire suppression windows that have ended — send NEWS_CLEAR to Layer 3.
    with _news_suppressed_lock:
        expired = [t for t, end in _news_suppressed_pairs.items() if end <= now]
    for t in expired:
        with _news_suppressed_lock:
            _news_suppressed_pairs.pop(t, None)
        _dispatch_news_clear(t)
        logger.info("NEWS suppression window closed for %s", t)

    # ── Two-stage news-first scan ─────────────────────────────────────────
    # Outer loop: events. Inner loop: pairs affected by that event's currency.
    #
    # Stage 1 (awareness zone, 31–60 min before): log only, no action.
    # Stage 2 (ban zone, 0–30 min before + 0–30 min after): close + suppress ONCE.
    for event in events:
        if event.get("impact") != "High":
            continue

        event_utc = event.get("time_utc")
        if event_utc is None:
            continue

        time_to_event = event_utc - now   # positive = upcoming, negative = past
        mins_to_event = time_to_event.total_seconds() / 60

        # Skip events outside the awareness window and beyond the post-event ban.
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

            # Stage 2 — ban zone (≤30 min away or ≤30 min past): close + suppress ONCE.
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

            # Tell Layer 3 to refuse new execution tickets for this pair.
            _dispatch_news_suppress(ticker, suppression_end)

            # Close any existing positions for this pair.
            if mins_to_event >= 0:
                direction = f"in {int(mins_to_event)} min"
            else:
                direction = f"{int(abs(mins_to_event))} min ago"
            event_desc = (
                f"[{event['currency']}] {event['title']} "
                f"@ {event_utc.strftime('%Y-%m-%d %H:%M')} UTC ({direction})"
            )
            logger.warning("NEWS BAN %s — %s", ticker, event_desc)
            _dispatch_close_ticker(ticker, f"pre_news_{ticker}")
            _alert_sync(
                f"<b>News Pre-Close — {ticker}</b>\n\n"
                f"{event_desc}\n\n"
                f"Positions closed. New signals blocked until "
                f"{suppression_end.strftime('%H:%M')} UTC "
                f"(event +{_NEWS_TRADING_BAN_WINDOW} min)."
            )


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

_last_curfew_close_date: date | None = None

_prop_fail_count:      int  = 0
_pers_fail_count:      int  = 0
_prop_down:            bool = False
_pers_down:            bool = False
_WORKER_DOWN_THRESHOLD: int = 3   # consecutive 30 s misses before alert (~90 s)


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
    global _prop_fail_count, _pers_fail_count, _prop_down, _pers_down

    with _state_lock:
        p_halt = _phase_state.get("permanently_halted", False)
    if p_halt:
        return

    now_sgt  = _sgt_now()
    curfew   = now_sgt.hour < 12 or now_sgt.weekday() >= 5
    today    = now_sgt.date()

    if curfew:
        if _last_curfew_close_date != today:
            logger.info("Monitor: SGT curfew transition — dispatching force-close (positions only)")
            _dispatch_force_close("sgt_curfew", halt=False)
            _alert_sync("<b>SGT Curfew</b> — All positions closed.\nResumes 12:00 SGT on next weekday.")
            _last_curfew_close_date = today
        return

    # ── Worker health checks (run every cycle, independent of active state) ──
    try:
        _eq_result  = _query_equity(ZMQ_REQ_PROP, "")
        prop_equity = _eq_result["equity"]
        if _prop_down:
            _prop_down = False
            _prop_fail_count = 0
            _alert_sync(
                "<b>Worker Prop — Back Online</b>\n\n"
                "VPS #2 (worker-prop) is responding again."
            )
        else:
            _prop_fail_count = 0
    except Exception as exc:
        _prop_fail_count += 1
        logger.warning("Monitor: prop equity query failed (%d/%d): %s",
                       _prop_fail_count, _WORKER_DOWN_THRESHOLD, exc)
        if _prop_fail_count >= _WORKER_DOWN_THRESHOLD and not _prop_down:
            _prop_down = True
            _alert_sync(
                "<b>Worker Prop — OFFLINE</b>\n\n"
                f"VPS #2 not responding for ~{_WORKER_DOWN_THRESHOLD * 30}s.\n\n"
                "<b>Action:</b>\n"
                "1. Open VPS #2 noVNC\n"
                "2. <code>cd C:/arbitrage</code>\n"
                "3. <code>uv run python layer3/worker_prop.py</code>"
            )
        return

    try:
        _query_equity(ZMQ_REQ_PERS, "")
        if _pers_down:
            _pers_down = False
            _pers_fail_count = 0
            _alert_sync(
                "<b>Worker Personal — Back Online</b>\n\n"
                "VPS #3 (worker-personal) is responding again."
            )
        else:
            _pers_fail_count = 0
    except Exception as exc:
        _pers_fail_count += 1
        logger.warning("Monitor: personal equity query failed (%d/%d): %s",
                       _pers_fail_count, _WORKER_DOWN_THRESHOLD, exc)
        if _pers_fail_count >= _WORKER_DOWN_THRESHOLD and not _pers_down:
            _pers_down = True
            _alert_sync(
                "<b>Worker Personal — OFFLINE</b>\n\n"
                f"VPS #3 not responding for ~{_WORKER_DOWN_THRESHOLD * 30}s.\n\n"
                "<b>Action:</b>\n"
                "1. Open VPS #3 noVNC\n"
                "2. <code>cd C:/arbitrage</code>\n"
                "3. <code>uv run python layer3/worker_personal.py</code>"
            )
        # personal failure doesn't block kill-condition checks — prop equity already fetched

    # Position mismatch check — runs every cycle when both workers are online
    if not _prop_down and not _pers_down:
        try:
            prop_pos = _query_positions(ZMQ_REQ_PROP)
            pers_pos = _query_positions(ZMQ_REQ_PERS)
            _run_mismatch_check(prop_pos, pers_pos)
        except Exception as exc:
            logger.warning("Mismatch check error: %s", exc)

    with _state_lock:
        active = _phase_state.get("active", False)
        phase  = int(_phase_state.get("phase", 1))

    if not active:
        return

    with _pf_lock:
        pf = dict(_propfirm)

    # Reset day-start equity when the prop firm's 11:00 SGT window rolls over
    stored_date = pf.get("day_start_date_utc", "")
    if stored_date != _propfirm_day(now_sgt):
        # Lock completed day's profit into consistency log (Phase 2 only)
        if phase == 2 and stored_date:
            day_profit = prop_equity - pf.get("day_start_equity", prop_equity)
            _record_day_profit(stored_date, day_profit)
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
        msg = (
            f"<b>KILL 1 — Daily Loss Limit Hit</b>\n\n"
            f"Daily loss: <b>{daily_loss_pct:.2f}%</b> ≥ {pf['max_drawdown_daily_pct']}%\n"
            f"Equity: <b>{prop_equity:.2f}</b>\n"
            f"All positions closed. System halted.\n\n"
            f"<b>Next steps:</b>\n"
            f"/resume — resume trading tomorrow\n"
            f"/changepropfirm — switch to a new prop firm account"
        )
        logger.warning(msg)
        _dispatch_force_close("daily_loss_limit", halt=True)
        _alert_sync(msg)
        return

    # Kill 2 — overall drawdown (all phases) — exact user-input threshold, no buffer
    # Prop firm closes account at this exact %. Personal positions become unhedged → close both + permanent halt.
    if baseline > 0:
        overall_dd_limit = pf.get("max_drawdown_overall_pct", 0.0)
        if overall_dd_limit > 0:
            overall_loss_pct = (baseline - prop_equity) / baseline * 100
            if overall_loss_pct >= overall_dd_limit:
                floor = round(baseline * (1.0 - overall_dd_limit / 100.0), 2)
                msg = (
                    f"<b>KILL 2 — Overall Drawdown Limit Hit</b>\n\n"
                    f"Overall loss: <b>{overall_loss_pct:.2f}%</b> ≥ {overall_dd_limit}%\n"
                    f"Baseline: {baseline:.2f}  |  Floor: {floor:.2f}  |  Equity: <b>{prop_equity:.2f}</b>\n"
                    f"Prop firm account blown. All positions closed. <b>Permanent halt.</b>\n\n"
                    f"<b>Next steps:</b>\n"
                    f"Buy a new prop firm challenge, then run /changepropfirm → /phase1 → /resume"
                )
                logger.warning(msg)
                _dispatch_force_close("overall_drawdown_limit", halt=True, permanent=True)
                _alert_sync(msg)
                return

    # Kill 3 — daily profit cap (all phases) — prop firm consistency rule
    daily_profit_pct = (prop_equity - day_start) / day_start * 100
    cap = pf.get("daily_profit_cap_pct", 0.0)
    if cap > 0 and daily_profit_pct >= cap:
        msg = (
            f"<b>KILL 3 — Daily Profit Cap Hit</b>\n\n"
            f"Daily profit: <b>{daily_profit_pct:.2f}%</b> ≥ {cap}%\n"
            f"Equity: <b>{prop_equity:.2f}</b>\n"
            f"All positions closed for today. Prop firm consistency rule enforced.\n\n"
            f"<b>Next steps:</b>\n"
            f"/resume — resume trading tomorrow"
        )
        logger.warning(msg)
        _dispatch_force_close("daily_profit_cap", halt=True)
        _alert_sync(msg)
        return

    # Kill 4 — profit target reached (all phases) — cumulative from baseline
    if baseline > 0:
        overall_pct = (prop_equity - baseline) / baseline * 100
        target      = pf.get("profit_target_pct", 0.0)
        if target > 0 and overall_pct >= target:
            if phase == 1:
                msg = (
                    f"<b>KILL 4 — Evaluation PASSED! Phase 1 Complete.</b>\n\n"
                    f"Overall profit: <b>{overall_pct:.2f}%</b> ≥ {target}%\n"
                    f"Equity: <b>{prop_equity:.2f}</b>\n"
                    f"All positions closed. System halted.\n\n"
                    f"<b>Ready to move to funded phase?</b>\n"
                    f"/phase2 — configure and start Phase 2\n"
                    f"/changepropfirm — start a new challenge instead"
                )
            else:
                msg = (
                    f"<b>KILL 4 — Phase {phase} Target Reached!</b>\n\n"
                    f"Overall profit: <b>{overall_pct:.2f}%</b> ≥ {target}%\n"
                    f"Equity: <b>{prop_equity:.2f}</b>\n"
                    f"All positions closed. System halted.\n\n"
                    f"<b>Options:</b>\n"
                    f"/phase2 — start a new challenge (wizard will ask for settings)\n"
                    f"/stop — end trading on this account"
                )
            logger.warning(msg)
            _dispatch_force_close("profit_target", halt=True, permanent=True)
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
                firm = pf.get("propfirm_name", "prop firm")
                overall_pct = total / baseline * 100 if baseline > 0 else 0.0
                msg = (
                    f"<b>KILL 5 — Consistency Rule Met</b>\n\n"
                    f"All positions closed. Trading halted.\n\n"
                    f"<pre>{table_str}</pre>\n\n"
                    f"<b>Overall profit: {overall_pct:.2f}%</b> across {len(locked_days) + (1 if today_running > 0 else 0)} days\n\n"
                    f"<b>Action required:</b>\n"
                    f"Log in to <b>{firm}</b> and submit your\n"
                    f"profit share withdrawal claim now.\n\n"
                    f"Send /phase2 + /resume to start a new cycle."
                )
                logger.warning("KILL 5 — consistency rule met: %.1f%% < %.1f%%", ratio_pct, cons_threshold)
                _dispatch_force_close("consistency_rule", halt=True, permanent=True)
                _alert_sync(msg)


# ── Telegram wizard — /changepropfirm ────────────────────────────────────

(PF_NAME, PF_PROFIT_TARGET, PF_MAX_DD_OVERALL, PF_MAX_DD_DAILY,
 PF_DD_TYPE, PF_RAW_SPREAD, PF_PROFIT_SHARE, PF_MIN_DAYS,
 PF_CONSISTENCY, PF_CONFIRM) = range(10)

(P2_SAME_OR_DIFF, P2_WHICH_FIELDS, P2_COLLECTING, P2_CONFIRM) = range(10, 14)

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
        f"<b>Profit Target:</b> {_wizard_data['profit_target_pct']}%\n"
        f"<b>Max DD Overall:</b> {_wizard_data['max_drawdown_overall_pct']}% → enforced at <b>{eff['max_drawdown_overall_pct']}%</b> (no buffer — exact)\n"
        f"<b>Max DD Daily:</b> {_wizard_data['max_drawdown_daily_pct']}% → enforced at <b>{eff['max_drawdown_daily_pct']}%</b> (−1pp buffer)\n"
        f"<b>Drawdown Type:</b> {'Static' if _wizard_data['drawdown_is_static'] else 'Dynamic'}{dd_flag}\n"
        f"<b>Raw Spread Acct:</b> {'Yes' if _wizard_data['raw_spread_account'] else 'No'}{rs_flag}\n"
        f"<b>Profit Sharing:</b> {_wizard_data['profit_sharing_pct']}%\n"
        f"<b>Min Profit Days:</b> {_wizard_data['min_profit_days']}\n"
        f"<b>Consistency Threshold:</b> {v}%\n\n"
        f"<b>Kill conditions:</b>\n"
        f"Kill 1 — daily loss ≥ {eff['max_drawdown_daily_pct']}% → close all + halt\n"
        f"Kill 2 — overall loss ≥ {eff['max_drawdown_overall_pct']}% from baseline → close all + <b>permanent halt</b>\n"
        f"Kill 3 — daily profit ≥ {eff['daily_profit_cap_pct']}% → close all + halt\n"
        f"Kill 4 — overall profit ≥ {_wizard_data['profit_target_pct']}% → permanent halt\n"
        f"Kill 5 — consistency: largest day &lt; {v}% of total → permanent halt <i>(Phase 2 only)</i>\n\n"
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

    _wizard_data.clear()
    await update.message.reply_text(
        f"<b>Config Saved</b>\n\n"
        f"<b>Firm:</b> {_propfirm['propfirm_name']}\n"
        f"<b>Baseline equity:</b> {baseline:.2f}\n\n"
        f"All kill-switch thresholds are now active.",
        parse_mode="HTML",
    )
    logger.info("Prop firm config updated — firm=%s  baseline=%.2f",
                _propfirm["propfirm_name"], baseline)
    return ConversationHandler.END


async def _wiz_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text("<b>Wizard Cancelled</b>\n\nNo changes saved.", parse_mode="HTML")
    return ConversationHandler.END


# ── Telegram commands ─────────────────────────────────────────────────────

async def _cmd_phase1(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
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
    await update.message.reply_text(
        f"<b>Phase 1 Active</b>\n\n"
        f"Personal lots multiplier: ×{PHASE_MULT[1]:.2f}\n"
        f"Baseline equity locked: <b>{balance:.2f}</b>\n\n"
        f"Send /resume to start trading.",
        parse_mode="HTML",
    )
    logger.info("Telegram: phase set to 1  baseline=%.2f", balance)


# ── Phase 2 setup wizard (/phase2) ───────────────────────────────────────

# Ordered field definitions used to display and collect Phase 2 settings.
# Each entry: (1-based index, config_key, display_name, input_type)
_P2_FIELD_DEFS = [
    (1, "propfirm_name",             "Propfirm name",              "str"),
    (2, "profit_target_pct",         "Profit target %",            "float_pos"),
    (3, "max_drawdown_overall_pct",  "Max DD overall %",           "float_pos"),
    (4, "max_drawdown_daily_pct",    "Max DD daily %",             "float_pos"),
    (5, "drawdown_is_static",        "Drawdown type",              "static_dynamic"),
    (6, "raw_spread_account",        "Raw spread account",         "yes_no"),
    (7, "profit_sharing_pct",        "Profit sharing %",           "float_pos"),
    (8, "min_profit_days",           "Min profit days",            "int_nn"),
    (9, "consistency_threshold_pct", "Consistency threshold %",    "float_pos"),
]
_P2_FIELD_BY_IDX: dict[int, tuple] = {d[0]: d for d in _P2_FIELD_DEFS}


def _p2_display(key: str, value) -> str:
    if key == "drawdown_is_static":
        return "Static" if value else "Dynamic"
    if key == "raw_spread_account":
        return "Yes" if value else "No"
    if key == "max_drawdown_daily_pct":
        return f"{value}% (enforced at {round(value - 1.0, 2)}% after −1pp buffer)"
    if key in ("profit_target_pct", "max_drawdown_overall_pct",
               "profit_sharing_pct", "consistency_threshold_pct"):
        return f"{value}%"
    return str(value)


def _p2_settings_block(cfg: dict, compare_to: dict | None = None) -> str:
    lines = []
    for idx, key, name, _ in _P2_FIELD_DEFS:
        val  = cfg.get(key, "—")
        mark = ""
        if compare_to is not None and compare_to.get(key) != val:
            mark = "  ← changed"
        lines.append(f"{idx}. {name:<22} — {_p2_display(key, val)}{mark}")
    return "\n".join(lines)


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
        f"Kill 1 — daily loss ≥ {eff['max_drawdown_daily_pct']}%\n"
        f"Kill 2 — overall loss ≥ {eff['max_drawdown_overall_pct']}%\n"
        f"Kill 3 — daily profit ≥ {eff['daily_profit_cap_pct']}%\n"
        f"Kill 4 — overall profit ≥ {new['profit_target_pct']}%\n"
        f"Kill 5 — consistency: largest day &lt; {new.get('consistency_threshold_pct', 29.0)}% of total → permanent halt\n"
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

    _p2_wizard_data.clear()
    await update.message.reply_text(
        f"<b>Phase 2 Active</b>\n\n"
        f"Firm: {_propfirm['propfirm_name']}\n"
        f"Personal lots multiplier: ×{PHASE_MULT[2]:.2f}\n"
        f"Baseline equity locked: <b>{baseline:.2f}</b>\n\n"
        f"Send /resume to start trading.",
        parse_mode="HTML",
    )
    logger.info("Phase 2 started — firm=%s  baseline=%.2f", _propfirm["propfirm_name"], baseline)
    return ConversationHandler.END


async def _p2_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _p2_wizard_data.clear()
    await update.message.reply_text("<b>Phase 2 Setup Cancelled.</b>", parse_mode="HTML")
    return ConversationHandler.END


async def _cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    with _state_lock:
        _phase_state["active"] = False
        _save_phase(_phase_state)
    await update.message.reply_text(
        "<b>Signal Processing Halted</b>\n\nSend /resume to re-enable.",
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
            "<b>Blocked</b> — Profit target was reached. Send /phase2 to configure and start the next phase.",
            parse_mode="HTML",
        )
        return
    with _state_lock:
        _phase_state["active"] = True
        _save_phase(_phase_state)
    curfew_note = "\n\n<i>Note: SGT curfew active — signals will be processed from 12:00 SGT.</i>" if _is_sgt_curfew() else ""
    await update.message.reply_text(
        f"<b>Signal Processing Resumed</b>{curfew_note}",
        parse_mode="HTML",
    )
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
    await update.message.reply_text(
        f"<b>System Status</b>\n\n"
        f"<b>Phase:</b> {phase}  (×{mult})\n"
        f"<b>Active:</b> {'YES' if active else 'NO — halted'}\n"
        f"<b>Perm Halt:</b> {'YES — /phase2 required' if p_halt else 'No'}\n"
        f"<b>SGT Curfew:</b> {'YES (dormant)' if curfew else 'No'}\n"
        f"<b>Max open positions:</b> {max_pos}\n"
        f"<b>Firm:</b> {pf_name}\n\n"
        f"<b>Equity</b>\n"
        f"Baseline:         {baseline:.2f}\n"
        f"DD floor:         {floor:.2f}  (−{dd_overall}% from baseline)\n"
        f"Day-start:        {day_start:.2f}\n"
        f"Daily DD limit:   {dd_daily}%\n"
        f"Daily profit cap: {cap}%\n\n"
        f"<b>Last signal:</b> {last_ts}",
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
        f"<b>Profit Target:</b> {pf.get('profit_target_pct', 0)}%\n"
        f"<b>Max DD Overall:</b> {pf.get('max_drawdown_overall_pct', 0)}%  (buffered)\n"
        f"<b>Max DD Daily:</b> {pf.get('max_drawdown_daily_pct', 0)}%  (buffered)\n"
        f"<b>Drawdown Type:</b> {'Static' if pf.get('drawdown_is_static') else 'Dynamic'}\n"
        f"<b>Raw Spread Acct:</b> {'Yes' if pf.get('raw_spread_account') else 'No'}\n"
        f"<b>Profit Sharing:</b> {pf.get('profit_sharing_pct', 0)}%\n"
        f"<b>Min Profit Days:</b> {pf.get('min_profit_days', 0)}\n"
        f"<b>Daily Profit Cap:</b> {pf.get('daily_profit_cap_pct', 0)}%\n"
        f"<b>Baseline Equity:</b> {pf.get('baseline_equity', 0):.2f}\n"
        f"<b>Day-Start Equity:</b> {pf.get('day_start_equity', 0):.2f}",
        parse_mode="HTML",
    )


async def _cmd_equity(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        prop = await asyncio.to_thread(_query_equity, ZMQ_REQ_PROP, "")
        prop_text = f"Balance: {prop['balance']:.2f}  |  Equity: {prop['equity']:.2f}"
    except Exception as exc:
        prop_text = f"OFFLINE — {exc}"
    try:
        pers = await asyncio.to_thread(_query_equity, ZMQ_REQ_PERS, "")
        pers_text = f"Balance: {pers['balance']:.2f}  |  Equity: {pers['equity']:.2f}"
    except Exception as exc:
        pers_text = f"OFFLINE — {exc}"
    await update.message.reply_text(
        f"<b>Live Equity Snapshot</b>\n\n"
        f"<b>Prop (VPS #2):</b>\n{prop_text}\n\n"
        f"<b>Personal (VPS #3):</b>\n{pers_text}",
        parse_mode="HTML",
    )


async def _cmd_emergency(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    await asyncio.to_thread(_dispatch_force_close, "emergency_halt", halt=True)
    await update.message.reply_text(
        "<b>EMERGENCY HALT EXECUTED</b>\n\n"
        "All positions force-closed on both MT5 accounts.\n"
        "Signal processing halted.\n\n"
        "Send /resume to restart trading.",
        parse_mode="HTML",
    )
    logger.warning("Telegram: emergency halt executed by user")


def _query_positions(zmq_url: str) -> list[dict]:
    sock = _zmq_ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(zmq_url)
    try:
        sock.send_json({"query": "positions"})
        if not sock.poll(EQUITY_TIMEOUT):
            raise RuntimeError(f"positions query timed out ({zmq_url})")
        reply = sock.recv_json()
        return reply.get("positions", [])
    finally:
        sock.close()


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
                    f"  P&amp;L: ${p['profit']:.2f}"
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
        return f"{abs(val)/lim*100:.1f}%" if lim > 0 else "n/a"

    await update.message.reply_text(
        f"<b>P&amp;L Dashboard (Prop Account)</b>\n\n"
        f"Baseline:     ${baseline:,.2f}\n"
        f"Day started:  ${day_start:,.2f}\n"
        f"Now:          ${equity:,.2f}\n\n"
        f"<b>Daily P&amp;L:</b>  ${daily_pnl:+,.2f}\n"
        f"  Profit cap  ${cap_lim:,.2f}  ({_pct(daily_pnl, cap_lim)} used)\n"
        f"  DD limit   -${dd_day_lim:,.2f}  ({_pct(-daily_pnl, dd_day_lim)} used)\n\n"
        f"<b>Overall P&amp;L:</b> ${overall_pnl:+,.2f}\n"
        f"  Target      ${target_lim:,.2f}  ({_pct(overall_pnl, target_lim)} used)\n"
        f"  DD limit   -${dd_all_lim:,.2f}  ({_pct(-overall_pnl, dd_all_lim)} used)",
        parse_mode="HTML",
    )


async def _cmd_health(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=3.0) as client:
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


async def _cmd_suppressed(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text("No pairs currently suppressed.")
        return

    for ticker in sorted(all_pairs):
        reasons = []
        if ticker in news_active:
            ends_sgt = (news_active[ticker] + sgt_off).strftime("%H:%M SGT")
            reasons.append(f"news (until {ends_sgt})")
        if ticker in manual_active:
            reasons.append("manual /closepair")
        lines.append(f"{ticker}: {', '.join(reasons)}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def _cmd_closepair(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth(update):
        return
    text = (update.message.text or "").strip().split()
    if len(text) < 2:
        await update.message.reply_text("Usage: /closepair EURUSD")
        return
    ticker = text[1].upper()
    if ticker not in ALLOWED_PAIRS:
        await update.message.reply_text(
            f"Unknown pair: {ticker}\nAllowed: {', '.join(sorted(ALLOWED_PAIRS))}"
        )
        return

    await asyncio.to_thread(_dispatch_close_ticker, ticker, "manual_closepair")
    with _manual_suppress_lock:
        _manual_suppressed_pairs.add(ticker)
    _dispatch_news_suppress(ticker, datetime(9999, 12, 31, tzinfo=timezone.utc))

    await update.message.reply_text(
        f"<b>{ticker} closed and blocked.</b>\n\n"
        f"All {ticker} positions closed on both accounts.\n"
        f"New {ticker} signals suppressed until /resumepair {ticker}.",
        parse_mode="HTML",
    )
    logger.warning("Manual closepair: %s", ticker)


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
        _phase_state["max_open_positions"] = n
        _save_phase(_phase_state)

    warning = ""
    if n > 5:
        theoretical = round(n * PROP_RISK_PCT * 100, 2)
        with _pf_lock:
            dd_daily_raw = _propfirm.get("max_drawdown_daily_pct", 0.0) + 1.0  # before buffer
        warning = (
            f"\n\n<b>Warning:</b> {n} positions × {PROP_RISK_PCT*100:.2f}% = "
            f"<b>{theoretical:.2f}% theoretical max daily loss</b> if all SLs hit simultaneously.\n"
            f"Daily DD limit (before buffer): {dd_daily_raw:.1f}%"
        )

    await update.message.reply_text(
        f"<b>Max open positions set to {n}.</b>{warning}",
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
        "/emergency — Force-close ALL positions on both accounts + halt\n\n"
        "<b>Phase &amp; Trading Control</b>\n"
        "/phase1 — Phase 1 (×0.20 lots, evaluation) — runs 8-step config wizard\n"
        "/phase2 — Next phase (×0.70 lots) — wizard: same as Phase 1 or update settings\n"
        "/resume — Resume signal processing\n"
        "/stop — Halt signal processing (open trades continue to SL/TP)\n\n"
        "<b>Position Limits</b>\n"
        "/setmaxpos 2 — Set max simultaneous open trades (1–10)\n"
        "/maxpos — Show current limit and open count\n\n"
        "<b>Pair Control</b>\n"
        "/closepair EURUSD — Close all positions for pair + block new signals\n"
        "/resumepair EURUSD — Unblock pair and allow new signals\n\n"
        "<b>Status &amp; Monitoring</b>\n"
        "/positions — Open positions on both accounts\n"
        "/equity — Live balance + equity on both accounts\n"
        "/pnl — Today's P&amp;L vs daily cap and DD limits\n"
        "/consistency — Consistency rule tracker (Phase 2 only)\n"
        "/health — Ping all 4 layers\n"
        "/news — Upcoming high-impact events (next 4h)\n"
        "/suppressed — Active suppression blackboard\n"
        "/status — Live system status\n"
        "/propfirm — Current prop firm config\n"
        "/changepropfirm — Set up new prop firm (9-step wizard)\n"
        "/cancel — Cancel wizard mid-flow\n\n"
        "<b>Kill Conditions</b> (automatic)\n"
        "Kill 1 — daily loss ≥ DD daily limit → close all + halt\n"
        "Kill 2 — overall loss ≥ DD overall limit → close all + permanent halt\n"
        "Kill 3 — daily profit ≥ cap → close all + halt\n"
        "Kill 4 — overall profit ≥ target → close all + permanent halt → /phase2\n"
        "Kill 5 — consistency rule met (largest day &lt; threshold%) → close all + permanent halt → claim payout\n\n"
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

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(wizard)
    tg_app.add_handler(p2_wizard)
    tg_app.add_handler(CommandHandler("phase1",        _cmd_phase1))
    tg_app.add_handler(CommandHandler("stop",          _cmd_stop))
    tg_app.add_handler(CommandHandler("resume",        _cmd_resume))
    tg_app.add_handler(CommandHandler("status",        _cmd_status))
    tg_app.add_handler(CommandHandler("propfirm",      _cmd_propfirm))
    tg_app.add_handler(CommandHandler("equity",        _cmd_equity))
    tg_app.add_handler(CommandHandler("emergency",     _cmd_emergency))
    tg_app.add_handler(CommandHandler("changepropfirm", _cmd_changepropfirm))
    tg_app.add_handler(CommandHandler("positions",     _cmd_positions))
    tg_app.add_handler(CommandHandler("pnl",           _cmd_pnl))
    tg_app.add_handler(CommandHandler("health",        _cmd_health))
    tg_app.add_handler(CommandHandler("news",          _cmd_news))
    tg_app.add_handler(CommandHandler("suppressed",    _cmd_suppressed))
    tg_app.add_handler(CommandHandler("closepair",     _cmd_closepair))
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


_load_consistency_log()
threading.Thread(target=_run_bot,              daemon=True, name="tg-bot").start()
threading.Thread(target=_equity_monitor_loop,  daemon=True, name="equity-monitor").start()
threading.Thread(target=_news_preclose_loop,   daemon=True, name="news-preclose").start()

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
        reason  = "weekend" if now_sgt.weekday() >= 5 else "SGT curfew 00:00–12:00"
        logger.info("GATE %s %s — %s", payload.signal, payload.ticker, reason)
        return JSONResponse({"status": "rejected", "reason": reason})

    with _state_lock:
        active  = _phase_state.get("active", False)
        phase   = int(_phase_state.get("phase", 1))
        p_halt  = _phase_state.get("permanently_halted", False)
        max_pos = _phase_state.get("max_open_positions", 2)

    if p_halt:
        return JSONResponse({
            "status": "halted",
            "reason": "profit target reached — /phase2 to configure and start next phase",
        })

    if not active:
        logger.info("HALTED — dropped %s %s", payload.signal, payload.ticker)
        return JSONResponse({"status": "halted", "reason": "signal processing stopped"})

    # News + manual suppression gate
    now_utc = datetime.now(timezone.utc)
    with _news_suppressed_lock:
        news_block = payload.ticker in _news_suppressed_pairs and _news_suppressed_pairs[payload.ticker] > now_utc
    with _manual_suppress_lock:
        manual_block = payload.ticker in _manual_suppressed_pairs
    if news_block or manual_block:
        reason = "manual block (/closepair)" if manual_block else "news suppression window"
        logger.info("SUPPRESSED — dropped %s %s (%s)", payload.signal, payload.ticker, reason)
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

    prop_point    = prop_info["point"]
    prop_tick_val = prop_info["trade_tick_value"]

    if prop_point <= 0 or prop_tick_val <= 0:
        msg = f"Invalid contract data from prop worker for {payload.ticker} — point={prop_point} tick_value={prop_tick_val}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=503, detail=msg)

    if tp_distance <= 0:
        msg = f"TP distance is zero for {payload.ticker} — tp={payload.tp} entry={payload.entry}"
        logger.error(msg)
        await _telegram_alert(msg)
        raise HTTPException(status_code=422, detail=msg)

    # Funded account SL/TP are the exact swap of the personal account SL/TP:
    #   Funded SL = signal TP  (tight side, 54 pipettes)
    #   Funded TP = signal SL  (wide side, 200 pipettes)
    # This is direction-agnostic — same formula for BUY and SELL signals.
    price_digits = prop_info["digits"]
    prop_sl = round(payload.tp, price_digits)   # funded SL = signal TP
    prop_tp = round(payload.sl, price_digits)   # funded TP = signal SL

    # Lot sizing: funded account risks prop_dollar_risk if its SL hits.
    # Funded SL distance = tp_distance (signal TP − entry = 54 pipettes).
    # Formula: lots = dollar_risk / (funded_sl_distance / point × tick_value_per_lot)
    prop_dollar_per_lot = (tp_distance / prop_point) * prop_tick_val
    prop_lots            = round(prop_dollar_risk / prop_dollar_per_lot, 2)
    pers_lots            = round(prop_lots * phase_ratio, 2)
    pers_dollar_risk     = round(prop_dollar_risk * phase_ratio, 2)  # display only

    pers_tp = round(payload.tp, price_digits)   # personal TP = signal TP

    logger.info(
        "LOTS  prop=%.2f lots ($%.2f at TP)  personal=%.2f lots ($%.2f at TP)  "
        "phase=%d ×%.2f  baseline=%.2f  tp_dist=%.5f  sl_dist=%.5f  "
        "prop point=%.5f tick=%.4f",
        prop_lots, prop_dollar_risk, pers_lots, pers_dollar_risk,
        phase, phase_ratio, baseline_equity, tp_distance, sl_distance,
        prop_point, prop_tick_val,
    )

    # Personal follows signal direction; prop is inverse
    prop_ticket = {
        "ticker":       payload.ticker,
        "timestamp_ms": payload.timestamp_ms,
        "entry":        payload.entry,
        "sl":           prop_sl,                   # funded SL = signal TP
        "tp":           prop_tp,
        "sl_pips":      payload.sl_pips,
        "signal":       _invert(payload.signal),   # prop is inverse
        "lots":         prop_lots,
    }
    pers_ticket = {
        "ticker":       payload.ticker,
        "timestamp_ms": payload.timestamp_ms,
        "entry":        payload.entry,
        "sl":           payload.sl,                # personal uses webhook sl directly
        "tp":           pers_tp,
        "sl_pips":      payload.sl_pips,
        "signal":       payload.signal,            # personal follows signal
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

    await _telegram_alert(
        f"<b>Trade Fired — {payload.ticker}</b>\n\n"
        f"<b>Personal (signal):</b> {pers_ticket['signal']}  {pers_lots:.2f} lots\n"
        f"Entry: {payload.entry:.{price_digits}f}  "
        f"SL: {payload.sl:.{price_digits}f}  "
        f"TP: {pers_tp:.{price_digits}f}\n"
        f"Risk: ${pers_dollar_risk:.2f}\n\n"
        f"<b>Prop (inverse):</b> {prop_ticket['signal']}  {prop_lots:.2f} lots\n"
        f"Entry: {payload.entry:.{price_digits}f}  "
        f"SL: {prop_sl:.{price_digits}f}  "
        f"TP: {prop_tp:.{price_digits}f}\n"
        f"Risk: ${prop_dollar_risk:.2f}\n\n"
        f"Phase {phase}  |  Baseline: {baseline_equity:.2f}"
    )

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
