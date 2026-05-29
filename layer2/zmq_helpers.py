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
            "profit":             float(reply.get("profit",             0.0)),
            "trade_allowed":      bool(reply.get("trade_allowed",       True)),
            "point":              float(reply.get("point",              0.0)),
            "contract_size":      float(reply.get("contract_size",      0.0)),
            "trade_tick_size":    float(reply.get("trade_tick_size",    0.0)),
            "trade_tick_value":   float(reply.get("trade_tick_value",   0.0)),
            "digits":             int(reply.get("digits",               5)),
            "account_currency":   (reply.get("account_currency") or "USD"),
            "usd_to_acct_rate":   float(reply.get("usd_to_acct_rate",   1.0) or 1.0),
            "account_login":      reply.get("account_login"),
            "account_server":     reply.get("account_server"),
            "account_name":       reply.get("account_name"),
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
                "text": message,
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
                text=message,
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


def _snapshot_positions_str(pers_currency: str = "USD") -> str:
    """Query both accounts and return a formatted positions summary (sync, for kill alerts).

    MT5 reports position.profit in the account currency. Personal P&L renders
    in `pers_currency` (auto-detected — SGD on the live Fusion account); prop
    stays USD per the hard constraint.

    Output format (one line per position; one line per account if flat/offline):
        Personal: EURUSD ↑ LONG 0.10 -SGD 12.50
        Prop: No open positions
    """
    def _fmt_pnl(value: float, currency: str) -> str:
        sign = "+" if value >= 0 else "-"
        mag  = abs(value)
        if (currency or "USD").upper() == "USD":
            return f"{sign}${mag:,.2f}"
        return f"{sign}{currency} {mag:,.2f}"

    lines = []
    for label, url, ccy in [
        ("Personal", ZMQ_REQ_PERS, pers_currency),
        ("Prop",     ZMQ_REQ_PROP, "USD"),
    ]:
        try:
            positions = _query_positions(url)
            if positions:
                for p in positions:
                    arrow = "↑ LONG" if p["type"] == 0 else "↓ SHORT"
                    pnl   = _fmt_pnl(p["profit"], ccy)
                    lines.append(
                        f"{label}: {p['symbol']} {arrow} {p['volume']:.2f} {pnl}"
                    )
            else:
                lines.append(f"{label}: No open positions")
        except Exception:
            lines.append(f"{label}: OFFLINE")
    return "\n".join(lines)


# ── Equity day-start helpers ──────────────────────────────────────────────

def _update_day_start(equity: float) -> None:
    with _pf_lock:
        _propfirm["day_start_equity"]   = equity
        _propfirm["day_start_date_utc"] = _propfirm_day(_sgt_now())
        _save_propfirm(_propfirm)
    logger.info("Day-start equity set to %.2f", equity)


def _update_pers_day_start(equity: float) -> None:
    """Reset personal day-start equity at the daily 11:00 SGT rollover.
    Never touches pers_baseline_equity — that is set only via /setpersonalbaseline."""
    with _pf_lock:
        _propfirm["pers_day_start_equity"] = equity
        _save_propfirm(_propfirm)
    logger.info("Personal day-start equity set to %.2f", equity)


def _set_personal_baseline(amount: float) -> None:
    """Persist a user-supplied personal account baseline. Never auto-written by the bot."""
    with _pf_lock:
        _propfirm["pers_baseline_equity"] = amount
        _save_propfirm(_propfirm)
    logger.info("Personal baseline set to %.2f", amount)


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


def _query_deal_pnl(zmq_url: str, symbol: str) -> dict | None:
    """Query Layer 3 for the actual realized P&L of the most recently closed position on symbol.
    Returns the full reply dict (always includes account_mode, plus net_pnl/commission/close_price
    when found=True). Returns None only on transport error/timeout. Caller checks reply["found"]
    to decide whether to use the exact values or fall back to pos.profit."""
    sock = _zmq_ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(zmq_url)
    try:
        sock.send_json({"query": "deal_pnl", "symbol": symbol})
        if not sock.poll(EQUITY_TIMEOUT):
            return None
        return sock.recv_json()
    except Exception:
        return None
    finally:
        sock.close()


def _query_account_mode(zmq_url: str) -> str:
    """Query Layer 3 for the MT5 account mode (demo/real/contest).
    Reads account_info.trade_mode cached at worker startup — fully automatic, no env var needed.
    Returns 'unknown' on transport error."""
    sock = _zmq_ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(zmq_url)
    try:
        sock.send_json({"query": "account_mode"})
        if not sock.poll(EQUITY_TIMEOUT):
            return "unknown"
        reply = sock.recv_json()
        return reply.get("account_mode", "unknown")
    except Exception:
        return "unknown"
    finally:
        sock.close()


def _query_order_check(zmq_url: str, order: dict) -> dict:
    """Ask a Layer 3 worker whether a proposed market order would be accepted.

    `order` keys: ticker, signal ("LONG"/"SHORT"), lots, sl, tp.
    Returns the worker's verdict dict: {verdict: "ok"|"reject"|"transient", retcode,
    comment, margin, margin_free, ...}. On transport error returns verdict="transient"
    so a slow/unreachable check does not block a legitimate trade — the post-dispatch
    verify + orphan watcher remain as the safety net.
    """
    sock = _zmq_ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(zmq_url)
    try:
        sock.send_json({"query": "order_check", **order})
        if not sock.poll(EQUITY_TIMEOUT):
            return {"verdict": "transient", "comment": "order_check timed out", "retcode": None}
        return sock.recv_json()
    except Exception as exc:
        return {"verdict": "transient", "comment": str(exc), "retcode": None}
    finally:
        sock.close()


def _query_order_status(zmq_url: str, signal_id: str) -> dict:
    """Query Layer 3 for execution status of a pending/filled order by signal_id."""
    sock = _zmq_ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.connect(zmq_url)
    try:
        sock.send_json({"query": "order_status", "signal_id": signal_id})
        if not sock.poll(EQUITY_TIMEOUT):
            return {"status": "UNKNOWN", "error": "timeout"}
        return sock.recv_json()
    except Exception as exc:
        return {"status": "UNKNOWN", "error": str(exc)}
    finally:
        sock.close()
