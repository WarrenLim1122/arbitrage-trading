"""
Shared execution worker logic for Layer 3.

One instance runs on VPS #2 (prop — FundingPips) and one on
VPS #3 (personal — Fusion Markets). Configured entirely via env vars.

New in v2:
  - FORCE_CLOSE message type: closes all open positions on this MT5 account.
  - SGT kill switch thread: force-closes at midnight SGT, dormant until 12:00 SGT.
    Dormant window also covers weekends (Sat–Sun full day).
  - Weekday guard: incoming execution tickets are silently dropped while dormant.

Environment variables:
  WORKER_NAME    — "prop" or "personal"
  MT5_LOGIN      — integer MT5 account number
  MT5_PASSWORD   — MT5 account password
  MT5_SERVER     — MT5 broker server name
  ZMQ_PULL_ADDR  — execution ticket listener  (default tcp://0.0.0.0:5555)
  ZMQ_REP_ADDR   — equity query responder     (default tcp://0.0.0.0:5556)
  MT5_MAGIC      — EA magic number            (default 20250001)
"""

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import zmq
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────
WORKER_NAME  = os.getenv("WORKER_NAME", "worker")
MT5_LOGIN    = int(os.environ["MT5_LOGIN"])
MT5_PASSWORD = os.environ["MT5_PASSWORD"]
MT5_SERVER   = os.environ["MT5_SERVER"]
PULL_ADDR    = os.getenv("ZMQ_PULL_ADDR", "tcp://0.0.0.0:5555")
REP_ADDR     = os.getenv("ZMQ_REP_ADDR",  "tcp://0.0.0.0:5556")
MT5_MAGIC    = int(os.getenv("MT5_MAGIC", "20250001"))

JOURNAL_ENABLED = os.getenv("FIREBASE_JOURNAL_ENABLED", "false").lower() == "true"

MAX_RETRIES      = 3
RETRY_DELAY      = 0.5
DEVIATION_POINTS = 20
RECONNECT_DELAY  = 5

SGT = ZoneInfo("Asia/Singapore")

# ── Logging ───────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_log_file = (
    LOG_DIR
    / f"layer3_{WORKER_NAME}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger(f"layer3.{WORKER_NAME}")

# ── Locks and shared state ────────────────────────────────────────────────
_mt5_lock            = threading.Lock()
_dormant_lock        = threading.Lock()
_dd_params_lock      = threading.Lock()
_news_suppressed_lock = threading.Lock()
_dormant             = False             # True during 00:00–11:59 SGT and weekends

_filling_cache: dict[str, int] = {}
_last_curfew_close_date: date | None = None

# mt5 Python binding does not export SYMBOL_FILLING_* flag constants.
# Use raw bitmask values from the MT5 specification:
#   bit 0 (1) = symbol supports ORDER_FILLING_FOK
#   bit 1 (2) = symbol supports ORDER_FILLING_IOC
_FILLING_FLAG_FOK = 1
_FILLING_FLAG_IOC = 2

# News suppression guard — ticker → expiry epoch seconds.
# Populated by NEWS_SUPPRESS from Layer 2. Refuses execution tickets for
# suppressed pairs even if Layer 1/2 somehow let one through.
_news_suppressed: dict[str, float] = {}

# Static drawdown floor — sent from Layer 2 via SET_PARAMETERS, persisted locally
_dd_params: dict = {"dd_floor": 0.0}
DD_PARAMS_PATH = Path(__file__).parent.parent / "config" / "dd_floor.json"

LIMIT_ONLY_EXECUTION = os.getenv("LIMIT_ONLY_EXECUTION", "true").lower() == "true"

_execution_results: dict[str, dict] = {}
_exec_results_lock = threading.Lock()

# Position close watcher — tracks open positions to detect TP/SL closes
_known_positions: dict[int, dict] = {}
_known_positions_lock = threading.Lock()


def _load_dd_params() -> None:
    global _dd_params
    if DD_PARAMS_PATH.exists():
        with DD_PARAMS_PATH.open() as f:
            _dd_params = json.load(f)
        logger.info("Loaded dd_params from disk: %s", _dd_params)


def _save_dd_params() -> None:
    DD_PARAMS_PATH.parent.mkdir(exist_ok=True)
    with DD_PARAMS_PATH.open("w") as f:
        json.dump(_dd_params, f, indent=2)


# ── Symbol map — broker-specific ticker name resolution ───────────────────

SYMBOL_MAP_PATH = Path(__file__).parent.parent / "config" / "symbol_map.json"

_DEFAULT_SYMBOL_MAP: dict[str, str] = {
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDCHF": "USDCHF",
    "USDCAD": "USDCAD",
    "USDJPY": "USDJPY",
    "NZDUSD": "NZDUSD",
    "XAUUSD": "XAUUSD",
    "XAGUSD": "XAGUSD",
    "NAS100": "NAS100",
}

_symbol_map: dict[str, str] = {}


def _load_symbol_map() -> None:
    global _symbol_map
    if SYMBOL_MAP_PATH.exists():
        with SYMBOL_MAP_PATH.open() as f:
            _symbol_map = json.load(f)
        logger.info("Symbol map loaded: %s", _symbol_map)
    else:
        _symbol_map = dict(_DEFAULT_SYMBOL_MAP)
        SYMBOL_MAP_PATH.parent.mkdir(exist_ok=True)
        with SYMBOL_MAP_PATH.open("w") as f:
            json.dump(_symbol_map, f, indent=2)
        logger.info("Symbol map created with defaults: %s", _symbol_map)


def _resolve_symbol(canonical: str) -> str:
    """Map canonical ticker to this broker's actual MT5 symbol name."""
    return _symbol_map.get(canonical, canonical)


# ── MT5 connection ────────────────────────────────────────────────────────

# Cached at connect time — read from MT5 itself (account_info.trade_mode),
# never set manually. Values: "demo" | "real" | "contest" | "unknown".
_account_mode: str = "unknown"


def _resolve_account_mode(acct) -> str:
    """Map MT5 ACCOUNT_TRADE_MODE_* constant to a string label."""
    try:
        if   acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO:    return "demo"
        elif acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL:    return "real"
        elif acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_CONTEST: return "contest"
    except Exception:
        pass
    return "unknown"


def _connect_mt5() -> None:
    global _account_mode
    while True:
        with _mt5_lock:
            ok = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if ok:
            with _mt5_lock:
                acct     = mt5.account_info()
                terminal = mt5.terminal_info()
            _account_mode = _resolve_account_mode(acct)
            logger.info("MT5 connected — account=%d  server=%s  balance=%.2f  mode=%s",
                        acct.login, acct.server, acct.balance, _account_mode)
            if not terminal.trade_allowed:
                logger.error(
                    "Automated trading DISABLED in MT5. "
                    "Enable via Tools → Options → Expert Advisors → Allow automated trading."
                )
            return
        logger.error("MT5 init failed (%s) — retrying in %ds", mt5.last_error(), RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


def _ensure_connected() -> None:
    with _mt5_lock:
        alive = mt5.terminal_info() is not None
    if not alive:
        logger.warning("MT5 terminal lost — reconnecting...")
        _connect_mt5()


# ── Symbol helpers ────────────────────────────────────────────────────────

def _contract_info(canonical: str) -> tuple[float, float, float, float, int]:
    """Return (point, contract_size, trade_tick_size, trade_tick_value, digits) for canonical ticker.

    All values come directly from mt5.symbol_info() after broker symbol resolution.
    Layer 2 lot sizing uses contract_size for xxxUSD pairs (price already in USD/unit,
    so dollar_per_lot = sl_distance × contract_size) and tick_size/tick_value for USDxxx
    pairs where the broker tick_value handles the foreign-currency conversion.
    """
    resolved = _resolve_symbol(canonical)
    with _mt5_lock:
        info = mt5.symbol_info(resolved)
    if info is None:
        raise RuntimeError(f"symbol_info returned None for {resolved} (canonical: {canonical})")
    return info.point, info.trade_contract_size, info.trade_tick_size, info.trade_tick_value, info.digits


def _get_filling_mode(resolved: str) -> int:
    """Takes the broker's actual symbol name (already resolved). Cached per symbol."""
    if resolved in _filling_cache:
        return _filling_cache[resolved]
    with _mt5_lock:
        flags = mt5.symbol_info(resolved).filling_mode
    if flags & _FILLING_FLAG_IOC:
        mode = mt5.ORDER_FILLING_IOC
    elif flags & _FILLING_FLAG_FOK:
        mode = mt5.ORDER_FILLING_FOK
    else:
        mode = mt5.ORDER_FILLING_RETURN
    _filling_cache[resolved] = mode
    return mode


# ── Force-close all open positions ───────────────────────────────────────

def _force_close_all(reason: str) -> None:
    _ensure_connected()
    # Cancel any pending orders (FORCE_CLOSE should abort waiting limit orders too)
    with _mt5_lock:
        pending = mt5.orders_get() or []
    for order in pending:
        if order.magic != MT5_MAGIC:
            continue
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket}
        with _mt5_lock:
            res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info("FORCE_CLOSE(%s): cancelled pending order %d %s",
                        reason, order.ticket, order.symbol)
        else:
            rc = res.retcode if res else "None"
            logger.error("FORCE_CLOSE(%s): failed to cancel pending %d retcode=%s",
                         reason, order.ticket, rc)
    with _mt5_lock:
        positions = mt5.positions_get()
    if not positions:
        logger.info("FORCE_CLOSE(%s): no open positions", reason)
        return

    closed = 0
    for pos in positions:
        # pos.type: 0 = BUY → close with SELL; 1 = SELL → close with BUY
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        with _mt5_lock:
            tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.error("FORCE_CLOSE: no tick for %s — skipping ticket=%d",
                         pos.symbol, pos.ticket)
            continue
        price    = tick.bid if pos.type == 0 else tick.ask
        filling  = _get_filling_mode(pos.symbol)
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     pos.ticket,
            "price":        price,
            "deviation":    DEVIATION_POINTS,
            "magic":        MT5_MAGIC,
            "comment":      f"TEE-FC-{reason[:8]}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        with _mt5_lock:
            result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info("FORCE_CLOSE(%s): closed %s ticket=%d  price=%.5f",
                        reason, pos.symbol, pos.ticket, result.price)
            closed += 1
        else:
            rc = result.retcode if result else "None"
            cm = result.comment if result else ""
            logger.error("FORCE_CLOSE(%s): FAILED %s ticket=%d  retcode=%s  %s",
                         reason, pos.symbol, pos.ticket, rc, cm)

    logger.info("FORCE_CLOSE(%s): %d/%d positions closed", reason, closed, len(positions))


# ── Close positions for a single ticker ──────────────────────────────────

def _force_close_ticker(canonical_ticker: str, reason: str) -> None:
    """Close all open positions for one specific symbol on this MT5 account."""
    resolved = _resolve_symbol(canonical_ticker)
    _ensure_connected()
    with _mt5_lock:
        positions = mt5.positions_get(symbol=resolved)
    if not positions:
        logger.info("CLOSE_TICKER(%s): no open positions for %s", reason, resolved)
        return

    # Tag positions as NEWS close so the journal pipeline knows the reason.
    if reason.startswith("pre_news"):
        with _known_positions_lock:
            for pos in positions:
                if pos.ticket in _known_positions:
                    _known_positions[pos.ticket]["close_reason_override"] = "NEWS"

    closed = 0
    for pos in positions:
        close_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        with _mt5_lock:
            tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.error("CLOSE_TICKER: no tick for %s — skipping ticket=%d",
                         pos.symbol, pos.ticket)
            continue
        price   = tick.bid if pos.type == 0 else tick.ask
        filling = _get_filling_mode(pos.symbol)
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     pos.ticket,
            "price":        price,
            "deviation":    DEVIATION_POINTS,
            "magic":        MT5_MAGIC,
            "comment":      f"TEE-NEWS-{reason[:8]}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        with _mt5_lock:
            result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info("CLOSE_TICKER(%s): closed %s ticket=%d  price=%.5f",
                        reason, pos.symbol, pos.ticket, result.price)
            closed += 1
        else:
            rc = result.retcode if result else "None"
            cm = result.comment if result else ""
            logger.error("CLOSE_TICKER(%s): FAILED %s ticket=%d  retcode=%s  %s",
                         reason, pos.symbol, pos.ticket, rc, cm)

    logger.info("CLOSE_TICKER(%s): %d/%d closed for %s", reason, closed, len(positions), resolved)


# ── SGT kill switch thread ────────────────────────────────────────────────

def _sgt_scheduler() -> None:
    global _dormant, _last_curfew_close_date

    while True:
        now_sgt  = datetime.now(SGT)
        weekday  = now_sgt.weekday()   # 0=Mon … 6=Sun
        today    = now_sgt.date()

        is_weekend = weekday >= 5      # Sat or Sun

        should_be_dormant = is_weekend

        with _dormant_lock:
            was_dormant = _dormant

        # Transition active → dormant: force-close once per calendar day (weekend entry)
        if not was_dormant and should_be_dormant:
            if _last_curfew_close_date != today:
                logger.info("SGT: entering weekend — force-closing all positions")
                _force_close_all("sgt_weekend")
                _last_curfew_close_date = today

        if should_be_dormant and _last_curfew_close_date != today:
            # Already dormant across a date boundary (e.g. multi-day weekend)
            _last_curfew_close_date = today

        with _dormant_lock:
            _dormant = should_be_dormant

        time.sleep(30)


# ── Order execution ───────────────────────────────────────────────────────

def _monitor_pending_order(
    signal_id: str, resolved: str, order_ticket: int,
    req_entry: float, req_sl: float, req_tp: float,
) -> None:
    """Background thread: poll until pending order reaches a terminal state."""
    MAX_WAIT = 14_400  # 4 hours
    start = time.time()

    while time.time() - start < MAX_WAIT:
        time.sleep(3)

        with _mt5_lock:
            still_active = mt5.orders_get(ticket=order_ticket)
        if still_active:
            continue  # Still pending — keep waiting

        # Order left active pool — check history
        with _mt5_lock:
            hist = mt5.history_orders_get(ticket=order_ticket)
        if not hist:
            time.sleep(1)
            with _mt5_lock:
                hist = mt5.history_orders_get(ticket=order_ticket)
        if not hist:
            continue  # MT5 history not updated yet

        h = hist[0]
        ts = datetime.now(timezone.utc).isoformat()

        if h.state == mt5.ORDER_STATE_FILLED:
            # Get actual fill price from the deal associated with this order
            fill_price = h.price_open  # default: requested price (limit orders fill at this)
            fill_volume = h.volume_initial - h.volume_current
            try:
                from_dt = datetime.now(timezone.utc) - timedelta(hours=24)
                to_dt   = datetime.now(timezone.utc) + timedelta(seconds=10)
                with _mt5_lock:
                    deals = mt5.history_deals_get(from_dt, to_dt) or []
                order_deals = [d for d in deals if getattr(d, "order", -1) == order_ticket]
                if order_deals:
                    fill_price  = order_deals[0].price
                    fill_volume = order_deals[0].volume
            except Exception as exc:
                logger.warning("Deal lookup failed for order %d: %s", order_ticket, exc)

            result = {
                "status":            "FILLED",
                "mt5_order_ticket":  order_ticket,
                "actual_fill_price": fill_price,
                "actual_volume":     fill_volume,
                "actual_sl":         h.sl,
                "actual_tp":         h.tp,
                "requested_entry":   req_entry,
                "requested_sl":      req_sl,
                "requested_tp":      req_tp,
                "entry_discrepancy": round(abs(fill_price - req_entry), 6),
                "sl_discrepancy":    round(abs((h.sl  or req_sl)  - req_sl),  6),
                "tp_discrepancy":    round(abs((h.tp  or req_tp)  - req_tp),  6),
                "broker_comment":    getattr(h, "comment", ""),
                "timestamp":         ts,
            }
            logger.info("Order %d FILLED @ %.5f (req %.5f) disc=%.6f",
                        order_ticket, fill_price, req_entry, result["entry_discrepancy"])
        else:
            state_map = {
                mt5.ORDER_STATE_CANCELED: "CANCELLED",
                mt5.ORDER_STATE_REJECTED: "REJECTED",
                mt5.ORDER_STATE_EXPIRED:  "EXPIRED",
            }
            status = state_map.get(h.state, "CANCELLED")
            result = {
                "status":           status,
                "mt5_order_ticket": order_ticket,
                "broker_comment":   getattr(h, "comment", ""),
                "timestamp":        ts,
            }
            logger.info("Order %d terminal state: %s", order_ticket, status)

        with _exec_results_lock:
            _execution_results[signal_id] = result
        return

    # Monitoring timeout (curfew/news should have cancelled the order before this)
    logger.warning("Order %d monitor timeout after 4h (signal_id=%s)", order_ticket, signal_id)
    with _exec_results_lock:
        _execution_results[signal_id] = {
            "status":           "EXPIRED",
            "mt5_order_ticket": order_ticket,
            "error":            "monitor_timeout_4h",
            "timestamp":        datetime.now(timezone.utc).isoformat(),
        }


def _execute_order(ticket: dict) -> None:
    signal_id = ticket.get("signal_id", "")
    ticker    = ticket["ticker"]
    resolved  = _resolve_symbol(ticker)
    signal    = ticket["signal"]
    lots      = float(ticket["lots"])
    entry     = float(ticket["entry"])
    sl        = float(ticket["sl"])
    tp        = float(ticket["tp"])

    _ensure_connected()

    with _mt5_lock:
        term = mt5.terminal_info()
    if term is not None and not term.trade_allowed:
        logger.error(
            "MT5 algo trading DISABLED — cannot execute %s %s. "
            "Fix: MT5 Tools → Options → Expert Advisors → uncheck "
            "'Disable algorithmic trading when the account has been changed'.",
            signal, ticker,
        )
        if signal_id:
            with _exec_results_lock:
                _execution_results[signal_id] = {
                    "status": "ERROR",
                    "error":  "algo_trading_disabled",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
        return

    filling = _get_filling_mode(resolved)

    force_market = ticket.get("order_type") == "market"
    if not LIMIT_ONLY_EXECUTION or force_market:
        # Market order: either LIMIT_ONLY_EXECUTION=false env override, or order_type=market in ticket
        with _mt5_lock:
            tick = mt5.symbol_info_tick(resolved)
        if tick is None:
            logger.error("symbol_info_tick returned None for %s — aborting", resolved)
            return
        price      = tick.ask if signal == "LONG" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if signal == "LONG" else mt5.ORDER_TYPE_SELL
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       resolved,
            "volume":       lots,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    DEVIATION_POINTS,
            "magic":        MT5_MAGIC,
            "comment":      f"TEE-{WORKER_NAME}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        with _mt5_lock:
            result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info("MARKET FILLED  %s %s  %.2f lots @ %.5f  order=%d",
                        signal, ticker, lots, result.price, result.order)
            if signal_id:
                with _exec_results_lock:
                    _execution_results[signal_id] = {
                        "status":            "FILLED",
                        "mt5_order_ticket":  result.order,
                        "actual_fill_price": result.price,
                        "actual_volume":     lots,
                        "actual_sl":         sl,
                        "actual_tp":         tp,
                        "requested_entry":   entry,
                        "requested_sl":      sl,
                        "requested_tp":      tp,
                        "entry_discrepancy": round(abs(result.price - entry), 6),
                        "sl_discrepancy":    0.0,
                        "tp_discrepancy":    0.0,
                        "broker_comment":    result.comment,
                        "timestamp":         datetime.now(timezone.utc).isoformat(),
                    }
        else:
            rc = result.retcode if result else "None"
            cm = result.comment if result else ""
            logger.error("Market order rejected — retcode=%s  %s | %s %s", rc, cm, signal, ticker)
            if signal_id:
                with _exec_results_lock:
                    _execution_results[signal_id] = {
                        "status":         "REJECTED",
                        "broker_retcode": str(rc),
                        "broker_comment": cm,
                        "timestamp":      datetime.now(timezone.utc).isoformat(),
                    }
        return

    # ── Limit-only execution (default) ────────────────────────────────────
    with _mt5_lock:
        tick = mt5.symbol_info_tick(resolved)
    if tick is None:
        logger.error("symbol_info_tick returned None for %s — aborting", resolved)
        if signal_id:
            with _exec_results_lock:
                _execution_results[signal_id] = {
                    "status": "ERROR", "error": "no_tick_data",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
        return

    if signal == "LONG":
        if entry >= tick.ask:
            msg = (f"UNSUPPORTED_LIMIT: BUY entry {entry:.5f} >= ask {tick.ask:.5f} "
                   f"for {ticker} — would need BUY STOP (not allowed in limit-only mode)")
            logger.warning(msg)
            if signal_id:
                with _exec_results_lock:
                    _execution_results[signal_id] = {
                        "status":          "UNSUPPORTED_LIMIT_SETUP",
                        "error":           msg,
                        "requested_entry": entry,
                        "current_price":   tick.ask,
                        "timestamp":       datetime.now(timezone.utc).isoformat(),
                    }
            return
        limit_type     = mt5.ORDER_TYPE_BUY_LIMIT
        order_type_str = "BUY_LIMIT"
    else:
        if entry <= tick.bid:
            msg = (f"UNSUPPORTED_LIMIT: SELL entry {entry:.5f} <= bid {tick.bid:.5f} "
                   f"for {ticker} — would need SELL STOP (not allowed in limit-only mode)")
            logger.warning(msg)
            if signal_id:
                with _exec_results_lock:
                    _execution_results[signal_id] = {
                        "status":          "UNSUPPORTED_LIMIT_SETUP",
                        "error":           msg,
                        "requested_entry": entry,
                        "current_price":   tick.bid,
                        "timestamp":       datetime.now(timezone.utc).isoformat(),
                    }
            return
        limit_type     = mt5.ORDER_TYPE_SELL_LIMIT
        order_type_str = "SELL_LIMIT"

    request = {
        "action":       mt5.TRADE_ACTION_PENDING,
        "symbol":       resolved,
        "volume":       lots,
        "type":         limit_type,
        "price":        entry,
        "sl":           sl,
        "tp":           tp,
        "magic":        MT5_MAGIC,
        "comment":      f"TEE-{WORKER_NAME}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    with _mt5_lock:
        result = mt5.order_send(request)

    success = result is not None and result.retcode in (
        mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED,
    )

    if not success:
        rc = result.retcode if result else "None"
        cm = result.comment if result else "order_send returned None"
        logger.error("Pending order REJECTED: retcode=%s  %s | %s %s %.2f lots @ %.5f",
                     rc, cm, signal, ticker, lots, entry)
        if signal_id:
            with _exec_results_lock:
                _execution_results[signal_id] = {
                    "status":         "REJECTED",
                    "broker_retcode": str(rc),
                    "broker_comment": cm,
                    "timestamp":      datetime.now(timezone.utc).isoformat(),
                }
        return

    order_ticket = result.order
    logger.info("Pending %s placed: %s %s %.2f lots @ %.5f  ticket=%d",
                order_type_str, signal, ticker, lots, entry, order_ticket)

    if signal_id:
        with _exec_results_lock:
            _execution_results[signal_id] = {
                "status":           "PENDING_PLACED",
                "mt5_order_type":   order_type_str,
                "mt5_order_ticket": order_ticket,
                "requested_entry":  entry,
                "requested_sl":     sl,
                "requested_tp":     tp,
                "requested_volume": lots,
                "timestamp":        datetime.now(timezone.utc).isoformat(),
            }
        threading.Thread(
            target=_monitor_pending_order,
            args=(signal_id, resolved, order_ticket, entry, sl, tp),
            daemon=True,
            name=f"monitor-{order_ticket}",
        ).start()


# ── Static drawdown guard (prop worker only) ──────────────────────────────

def _static_dd_guard_loop() -> None:
    """Independently monitors floating equity against the static DD floor.

    Compares live MT5 equity against the floor = baseline × (1 − dd_overall%).
    Fires FORCE_CLOSE if the floor is breached, without waiting for Layer 2.
    Only started when WORKER_NAME == 'prop'.
    """
    while True:
        time.sleep(30)
        try:
            with _dd_params_lock:
                floor = _dd_params.get("dd_floor", 0.0)
            if floor <= 0:
                continue
            with _mt5_lock:
                info = mt5.account_info()
            if info is None:
                continue
            if info.equity < floor:
                logger.warning(
                    "STATIC DD GUARD — equity %.2f < floor %.2f — closing all positions",
                    info.equity, floor,
                )
                _force_close_all("static_drawdown")
        except Exception as exc:
            logger.error("Static DD guard error: %s", exc)


# ── Position close watcher — triggers journal on TP/SL close ─────────────────

def _position_close_watcher() -> None:
    """
    Background thread: poll MT5 positions every 5 s, detect closes, trigger journal.

    Only positions opened with the correct MT5_MAGIC number are journaled.
    Execution-critical path is not touched — this runs in its own daemon thread.
    """
    while True:
        time.sleep(5)
        try:
            with _mt5_lock:
                current = mt5.positions_get() or []

            current_tickets = {p.ticket for p in current}

            # Update snapshot for new/modified positions
            with _known_positions_lock:
                for pos in current:
                    if pos.ticket not in _known_positions:
                        _known_positions[pos.ticket] = {
                            "ticket":     pos.ticket,
                            "symbol":     pos.symbol,
                            "type":       pos.type,       # 0=LONG 1=SHORT
                            "volume":     pos.volume,
                            "price_open": pos.price_open,
                            "sl":         pos.sl,
                            "tp":         pos.tp,
                            "magic":      pos.magic,
                            "open_time":  datetime.fromtimestamp(pos.time, tz=timezone.utc),
                        }
                    else:
                        # Keep SL/TP current in case they were modified
                        _known_positions[pos.ticket]["sl"] = pos.sl
                        _known_positions[pos.ticket]["tp"] = pos.tp

                closed_tickets = set(_known_positions.keys()) - current_tickets

            for ticket in closed_tickets:
                with _known_positions_lock:
                    snapshot = _known_positions.pop(ticket, None)
                if not snapshot:
                    continue
                if snapshot.get("magic") != MT5_MAGIC:
                    logger.debug(
                        "Position %d closed (magic=%d ≠ %d) — journal skipped",
                        ticket, snapshot.get("magic", 0), MT5_MAGIC,
                    )
                    continue

                # Stamp close time immediately — accurate to within one poll interval (5 s)
                snapshot["close_time_detected"] = datetime.now(timezone.utc)
                # Capture last tick price as close price estimate before the position data ages
                try:
                    with _mt5_lock:
                        tick = mt5.symbol_info_tick(snapshot["symbol"])
                    if tick:
                        # LONG fills at ask on open, closes at bid; SHORT is the reverse
                        snapshot["close_price_est"] = (
                            tick.bid if snapshot.get("type") == 0 else tick.ask
                        )
                except Exception:
                    pass

                logger.info(
                    "Position closed: ticket=%d  %s — triggering journal",
                    ticket, snapshot.get("symbol"),
                )
                threading.Thread(
                    target=_journal_closed_position,
                    args=(ticket, snapshot),
                    daemon=True,
                    name=f"journal-{ticket}",
                ).start()

        except Exception as exc:
            logger.error("Position close watcher error: %s", exc)


def _journal_closed_position(ticket: int, snapshot: dict) -> None:
    """Wraps the journaling pipeline — safe to call from a daemon thread."""
    try:
        from .journal.journaling_worker import handle_closed_position
        handle_closed_position(
            mt5_lock=_mt5_lock,
            mt5_account_id=str(MT5_LOGIN),
            worker_name=WORKER_NAME,
            position_ticket=ticket,
            pos_snapshot=snapshot,
        )
    except Exception as exc:
        logger.error("Journal error (ticket=%d): %s", ticket, exc)


# ── Socket bind with retry (handles Address in use after abrupt restart) ─────

def _bind_with_retry(sock: zmq.Socket, addr: str, max_attempts: int = 5, delay: float = 3.0) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            sock.bind(addr)
            return
        except zmq.error.ZMQError as exc:
            if "Address in use" in str(exc) and attempt < max_attempts:
                logger.warning("Port %s in use — retry %d/%d in %.0fs", addr, attempt, max_attempts - 1, delay)
                time.sleep(delay)
            else:
                raise


# ── REP thread — equity query responder ───────────────────────────────────

def _build_positions_reply() -> dict:
    try:
        with _mt5_lock:
            positions = mt5.positions_get()
        if not positions:
            return {"positions": []}
        result = []
        for p in positions:
            result.append({
                "symbol":     p.symbol,
                "type":       p.type,      # 0=LONG 1=SHORT
                "volume":     round(p.volume, 2),
                "price_open": p.price_open,
                "sl":         p.sl,
                "tp":         p.tp,
                "profit":     p.profit,
                "magic":      p.magic,
                "ticket":     p.ticket,
            })
        return {"positions": result}
    except Exception as exc:
        logger.error("positions reply error: %s", exc)
        return {"positions": [], "error": str(exc)}


def _build_equity_reply(ticker: str) -> dict:
    try:
        with _mt5_lock:
            acct      = mt5.account_info()
            term      = mt5.terminal_info()
            positions = mt5.positions_get() or []
        balance       = acct.balance      if acct else 0.0
        equity        = acct.equity       if acct else 0.0
        profit        = acct.profit       if acct else 0.0
        trade_allowed = bool(term.trade_allowed) if term else True

        if positions and balance == equity:
            logger.warning(
                "Balance and equity are identical (%.2f) while %d position(s) open — "
                "verify MT5 account_info().equity",
                balance, len(positions),
            )

        point = contract_size = tick_size = tick_value = 0.0
        digits = 5
        if ticker:
            try:
                point, contract_size, tick_size, tick_value, digits = _contract_info(ticker)
            except Exception as exc:
                logger.warning("contract_info failed for %s: %s", ticker, exc)

        return {
            "balance":            balance,
            "equity":             equity,
            "profit":             profit,
            "trade_allowed":      trade_allowed,
            "point":              point,
            "contract_size":      contract_size,
            "trade_tick_size":    tick_size,
            "trade_tick_value":   tick_value,
            "digits":             digits,
            "account_login":      acct.login  if acct else None,
            "account_server":     acct.server if acct else None,
            "account_name":       acct.name   if acct else None,
        }
    except Exception as exc:
        logger.error("equity reply error: %s", exc)
        return {
            "error": str(exc),
            "balance": 0.0, "equity": 0.0, "profit": 0.0,
            "trade_allowed": True,
            "point": 0.0, "contract_size": 0.0,
            "trade_tick_size": 0.0, "trade_tick_value": 0.0, "digits": 5,
            "account_login": None, "account_server": None, "account_name": None,
        }


def _build_order_status_reply(signal_id: str) -> dict:
    if not signal_id:
        return {"status": "UNKNOWN", "error": "no signal_id"}
    with _exec_results_lock:
        return dict(_execution_results.get(signal_id, {"status": "UNKNOWN"}))


def _build_account_mode_reply() -> dict:
    """Return cached MT5 account mode (queried from account_info.trade_mode at connect)."""
    return {"account_mode": _account_mode}


def _build_deal_pnl_reply(symbol: str) -> dict:
    """Return actual realized P&L (gross + commission + swap) for the most recently closed position on symbol.
    Always includes account_mode so Layer 2 can decide message format even when deal history is unavailable
    (MetaQuotes Demo lags 2-3h; Fusion Markets returns deals in <1s)."""
    base = {"account_mode": _account_mode}
    if not symbol:
        return {**base, "found": False, "error": "no symbol"}
    try:
        from_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        to_dt   = datetime.now(timezone.utc) + timedelta(seconds=30)
        with _mt5_lock:
            deals = mt5.history_deals_get(from_dt, to_dt) or []

        sym_exits = [
            d for d in deals
            if d.symbol == symbol and d.entry == mt5.DEAL_ENTRY_OUT
        ]
        if not sym_exits:
            return {**base, "found": False}

        latest_exit = max(sym_exits, key=lambda d: d.time)
        ticket      = latest_exit.position_id

        pos_deals  = [d for d in deals if d.position_id == ticket]
        gross_pnl  = sum(d.profit     for d in pos_deals)
        commission = sum(d.commission for d in pos_deals)
        swap       = sum(d.swap       for d in pos_deals)
        net_pnl    = gross_pnl + commission + swap

        # Map MT5 deal reason → close reason label
        deal_reason_map = {
            mt5.DEAL_REASON_TP:     "TP",
            mt5.DEAL_REASON_SL:     "SL",
            mt5.DEAL_REASON_EXPERT: "BOT_LOGIC",
            mt5.DEAL_REASON_MOBILE: "MANUAL",
            mt5.DEAL_REASON_CLIENT: "MANUAL",
        }
        close_reason = deal_reason_map.get(latest_exit.reason, "UNKNOWN")

        return {
            **base,
            "found":        True,
            "ticket":       ticket,
            "close_price":  latest_exit.price,
            "close_reason": close_reason,
            "gross_pnl":    round(gross_pnl,  2),
            "commission":   round(commission, 2),
            "swap":         round(swap,       2),
            "net_pnl":      round(net_pnl,    2),
        }
    except Exception as exc:
        logger.error("deal_pnl reply error for %s: %s", symbol, exc)
        return {**base, "found": False, "error": str(exc)}


def _rep_loop(ctx: zmq.Context) -> None:
    sock = ctx.socket(zmq.REP)
    _bind_with_retry(sock, REP_ADDR)
    logger.info("REP socket bound on %s", REP_ADDR)

    while True:
        try:
            raw = sock.recv()
        except Exception as exc:
            logger.error("REP recv failed: %s — reopening socket", exc)
            sock.close()
            sock = ctx.socket(zmq.REP)
            _bind_with_retry(sock, REP_ADDR)
            continue

        try:
            msg    = json.loads(raw)
            query  = msg.get("query", "equity")
            ticker = msg.get("ticker", "EURUSD")
        except Exception:
            query  = "equity"
            ticker = "EURUSD"

        if query == "positions":
            reply = _build_positions_reply()
        elif query == "order_status":
            reply = _build_order_status_reply(msg.get("signal_id", ""))
        elif query == "deal_pnl":
            reply = _build_deal_pnl_reply(msg.get("symbol", ""))
        elif query == "account_mode":
            reply = _build_account_mode_reply()
        else:
            reply = _build_equity_reply(ticker)
        try:
            sock.send_json(reply)
        except Exception as exc:
            logger.error("REP send failed: %s", exc)


# ── PULL loop — execution ticket receiver (main thread) ───────────────────

def _pull_loop(ctx: zmq.Context) -> None:
    sock = ctx.socket(zmq.PULL)
    _bind_with_retry(sock, PULL_ADDR)
    logger.info("PULL socket bound on %s", PULL_ADDR)

    while True:
        try:
            ticket = sock.recv_json()

            # FORCE_CLOSE action — bypasses dormant guard (always execute)
            if ticket.get("action") == "FORCE_CLOSE":
                reason = ticket.get("reason", "unknown")
                logger.warning("FORCE_CLOSE received — reason=%s", reason)
                _force_close_all(reason)
                continue

            # CLOSE_TICKER — close positions for one pair only (news pre-close)
            if ticket.get("action") == "CLOSE_TICKER":
                ticker = ticket.get("ticker", "")
                reason = ticket.get("reason", "unknown")
                logger.warning("CLOSE_TICKER received — ticker=%s  reason=%s", ticker, reason)
                if ticker:
                    _force_close_ticker(ticker, reason)
                continue

            # SET_PARAMETERS — update static DD floor sent by Layer 2 on phase change
            if ticket.get("action") == "SET_PARAMETERS":
                floor = float(ticket.get("dd_floor", 0.0))
                with _dd_params_lock:
                    _dd_params["dd_floor"] = floor
                    _save_dd_params()
                logger.info("SET_PARAMETERS received: dd_floor=%.2f", floor)
                continue

            # NEWS_SUPPRESS — Layer 2 flagged this pair as entering a news window.
            # Refuse all new execution tickets for it until suppression_end_utc.
            if ticket.get("action") == "NEWS_SUPPRESS":
                ticker  = ticket.get("ticker", "")
                end_iso = ticket.get("suppression_end_utc", "")
                if ticker and end_iso:
                    try:
                        expiry = datetime.fromisoformat(end_iso).timestamp()
                        with _news_suppressed_lock:
                            _news_suppressed[ticker] = expiry
                        logger.info("NEWS_SUPPRESS: %s suppressed until %s UTC", ticker, end_iso)
                    except Exception as exc:
                        logger.warning("NEWS_SUPPRESS parse error: %s", exc)
                continue

            # NEWS_CLEAR — suppression window ended; pair is tradeable again.
            if ticket.get("action") == "NEWS_CLEAR":
                ticker = ticket.get("ticker", "")
                if ticker:
                    with _news_suppressed_lock:
                        _news_suppressed.pop(ticker, None)
                    logger.info("NEWS_CLEAR: %s suppression lifted", ticker)
                continue

            # Dormant guard — drop execution tickets during curfew / weekend
            with _dormant_lock:
                dormant = _dormant
            if dormant:
                logger.info("DORMANT — dropped ticket %s %s",
                            ticket.get("signal"), ticket.get("ticker"))
                continue

            # News suppression guard — last line of defence before MT5 execution.
            # Cleans up expired entries on the fly; no separate cleanup thread needed.
            ticker    = ticket.get("ticker", "")
            now_epoch = time.time()
            with _news_suppressed_lock:
                expired = [t for t, ex in _news_suppressed.items() if ex <= now_epoch]
                for t in expired:
                    del _news_suppressed[t]
                suppressed_until = _news_suppressed.get(ticker, 0.0)
            if suppressed_until > now_epoch:
                logger.warning(
                    "NEWS GUARD — rejected %s %s (suppressed for %ds more)",
                    ticket.get("signal"), ticker, int(suppressed_until - now_epoch),
                )
                continue

            logger.info("TICKET  %s %s  %.2f lots",
                        ticket.get("signal"), ticket.get("ticker"), ticket.get("lots"))
            _execute_order(ticket)

        except Exception as exc:
            logger.error("PULL loop error: %s", exc)


# ── Entrypoint ────────────────────────────────────────────────────────────

def _ensure_symbols_in_market_watch() -> None:
    """Add all symbols from the symbol map to MT5 MarketWatch so tick data is available.

    symbol_info_tick() returns None for symbols not in MarketWatch, which causes
    _execute_order to abort silently. Calling symbol_select() at startup prevents this.
    """
    with _mt5_lock:
        for resolved in _symbol_map.values():
            ok = mt5.symbol_select(resolved, True)
            if not ok:
                logger.warning("symbol_select failed for %s — tick data may be unavailable", resolved)
            else:
                logger.info("symbol_select OK: %s", resolved)


def main() -> None:
    _load_symbol_map()
    _load_dd_params()
    _connect_mt5()
    _ensure_symbols_in_market_watch()
    ctx = zmq.Context.instance()
    threading.Thread(target=_rep_loop,      args=(ctx,), daemon=True, name="rep-equity").start()
    threading.Thread(target=_sgt_scheduler, daemon=True, name="sgt-scheduler").start()
    if WORKER_NAME == "prop":
        threading.Thread(target=_static_dd_guard_loop, daemon=True, name="dd-guard").start()
        logger.info("Static DD guard started (prop worker only)")
    if JOURNAL_ENABLED:
        from .journal.retry_queue import start_retry_worker
        from .journal.pending_deals_queue import start_pending_retry_worker
        threading.Thread(
            target=_position_close_watcher, daemon=True, name="pos-close-watcher"
        ).start()
        start_retry_worker()
        start_pending_retry_worker(_mt5_lock, str(MT5_LOGIN), WORKER_NAME)
        logger.info(
            "Journal modules started (dry_run=%s)",
            os.getenv("FIREBASE_JOURNAL_DRY_RUN", "true"),
        )
    _pull_loop(ctx)  # blocks in main thread
