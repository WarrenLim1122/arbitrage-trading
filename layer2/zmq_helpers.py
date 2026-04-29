import logging
from datetime import datetime

import httpx
import zmq
from telegram import Bot

from layer2.state import (
    _zmq_ctx, EQUITY_TIMEOUT, ZMQ_PUSH_PROP, ZMQ_PUSH_PERS, ZMQ_REQ_PROP, ZMQ_REQ_PERS,
    BOT_TOKEN, CHAT_ID,
    _phase_state, _state_lock, _save_phase,
    _pf_lock, _propfirm, _save_propfirm,
    _propfirm_day, _sgt_now,
)

logger = logging.getLogger("layer2")


def _query_equity(zmq_url: str, ticker: str) -> dict:
    """Query Layer 3 worker. Returns dict with keys:
      balance, equity, point, contract_size, trade_tick_size, trade_tick_value, digits

    Pass ticker="" for balance/equity-only queries (monitor, baseline lock).
    Pass the canonical ticker (e.g. "EURUSD") for signal handler contract queries.
    trade_tick_size (not point) must be used in lot sizing — they differ on some instruments.
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
            "balance":            float(reply.get("balance",            0.0)),
            "equity":             float(reply.get("equity",             0.0)),
            "trade_allowed":      bool(reply.get("trade_allowed",       True)),
            "point":              float(reply.get("point",              0.0)),
            "contract_size":      float(reply.get("contract_size",      0.0)),
            "trade_tick_size":    float(reply.get("trade_tick_size",    0.0)),
            "trade_tick_value":   float(reply.get("trade_tick_value",   0.0)),
            "digits":             int(reply.get("digits",               5)),
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


# ── Telegram alert (sync — safe to call from any thread) ──────────────────

def _alert_sync(message: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        with httpx.Client(timeout=5.0) as client:
            client.post(url, json={
                "chat_id": CHAT_ID,
                "text": f"🚨 <b>TEE Alert</b> 🚨\n\n{message}",
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
                text=f"🚨 <b>TEE Alert</b> 🚨\n\n{message}",
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


def _snapshot_positions_str() -> str:
    """Query both accounts and return a formatted positions summary (sync, for kill alerts)."""
    lines = []
    for label, url in [("Personal", ZMQ_REQ_PERS), ("Prop", ZMQ_REQ_PROP)]:
        try:
            positions = _query_positions(url)
            if positions:
                for p in positions:
                    arrow = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                    lines.append(
                        f"  {label}: {p['symbol']} {arrow} {p['volume']:.2f} lots"
                        f"  P&amp;L: ${p['profit']:+,.2f}"
                    )
            else:
                lines.append(f"  {label}: No open positions")
        except Exception:
            lines.append(f"  {label}: OFFLINE — could not query")
    return "\n".join(lines)


# ── Equity day-start helpers ──────────────────────────────────────────────

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
