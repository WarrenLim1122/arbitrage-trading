"""
Shared execution worker logic for Layer 3.

One instance runs on VPS #2 (prop — FundingPips) and one on
VPS #3 (personal — Fusion Markets). Configured entirely via env vars.

New in v2:
  - FORCE_CLOSE message type: closes all open positions on this MT5 account.
  - SGT kill switch thread: force-closes at midnight SGT, dormant until 09:00 SGT.
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
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import zmq

# ── Config ────────────────────────────────────────────────────────────────
WORKER_NAME  = os.getenv("WORKER_NAME", "worker")
MT5_LOGIN    = int(os.environ["MT5_LOGIN"])
MT5_PASSWORD = os.environ["MT5_PASSWORD"]
MT5_SERVER   = os.environ["MT5_SERVER"]
PULL_ADDR    = os.getenv("ZMQ_PULL_ADDR", "tcp://0.0.0.0:5555")
REP_ADDR     = os.getenv("ZMQ_REP_ADDR",  "tcp://0.0.0.0:5556")
MT5_MAGIC    = int(os.getenv("MT5_MAGIC", "20250001"))

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
_mt5_lock       = threading.Lock()
_dormant_lock   = threading.Lock()
_dd_params_lock = threading.Lock()
_dormant        = False             # True during 00:00–08:59 SGT and weekends

_filling_cache: dict[str, int] = {}
_last_curfew_close_date: date | None = None

# Static drawdown floor — sent from Layer 2 via SET_PARAMETERS, persisted locally
_dd_params: dict = {"dd_floor": 0.0}
DD_PARAMS_PATH = Path(__file__).parent.parent / "config" / "dd_floor.json"


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
    "XAUUSD":  "XAUUSD",
    "USDJPY":  "USDJPY",
    "BTCUSD":  "BTCUSD",
    "ETHUSD":  "ETHUSD",
    "FTSE100": "UK100",
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

def _connect_mt5() -> None:
    while True:
        with _mt5_lock:
            ok = mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if ok:
            with _mt5_lock:
                acct     = mt5.account_info()
                terminal = mt5.terminal_info()
            logger.info("MT5 connected — account=%d  server=%s  balance=%.2f",
                        acct.login, acct.server, acct.balance)
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

def _contract_info(canonical: str) -> tuple[float, float, float, int]:
    """Return (point, contract_size, trade_tick_value, digits) for canonical ticker.

    All values come directly from MT5 symbol_info after broker symbol resolution.
    Used by Layer 2 for universal lot sizing: lots = dollar_risk / (sl_distance/point * tick_value)
    """
    resolved = _resolve_symbol(canonical)
    with _mt5_lock:
        info = mt5.symbol_info(resolved)
    if info is None:
        raise RuntimeError(f"symbol_info returned None for {resolved} (canonical: {canonical})")
    return info.point, info.trade_contract_size, info.trade_tick_value, info.digits


def _get_filling_mode(resolved: str) -> int:
    """Takes the broker's actual symbol name (already resolved). Cached per symbol."""
    if resolved in _filling_cache:
        return _filling_cache[resolved]
    with _mt5_lock:
        flags = mt5.symbol_info(resolved).filling_mode
    if flags & mt5.SYMBOL_FILLING_IOC:
        mode = mt5.ORDER_FILLING_IOC
    elif flags & mt5.SYMBOL_FILLING_FOK:
        mode = mt5.ORDER_FILLING_FOK
    else:
        mode = mt5.ORDER_FILLING_RETURN
    _filling_cache[resolved] = mode
    return mode


# ── Force-close all open positions ───────────────────────────────────────

def _force_close_all(reason: str) -> None:
    _ensure_connected()
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


# ── SGT kill switch thread ────────────────────────────────────────────────

def _sgt_scheduler() -> None:
    global _dormant, _last_curfew_close_date

    while True:
        now_sgt  = datetime.now(SGT)
        h        = now_sgt.hour
        weekday  = now_sgt.weekday()   # 0=Mon … 6=Sun
        today    = now_sgt.date()

        in_curfew  = h < 9             # 00:00–08:59 SGT
        is_weekend = weekday >= 5      # Sat or Sun

        should_be_dormant = in_curfew or is_weekend

        with _dormant_lock:
            was_dormant = _dormant

        # Transition active → dormant: force-close once per calendar day
        if not was_dormant and should_be_dormant:
            if _last_curfew_close_date != today:
                logger.info("SGT: entering curfew/weekend — force-closing all positions")
                _force_close_all("sgt_curfew")
                _last_curfew_close_date = today

        if should_be_dormant and _last_curfew_close_date != today:
            # Already dormant across a date boundary (e.g. multi-day weekend)
            _last_curfew_close_date = today

        with _dormant_lock:
            _dormant = should_be_dormant

        time.sleep(30)


# ── Order execution ───────────────────────────────────────────────────────

def _execute_order(ticket: dict) -> None:
    receipt_ms = int(time.time() * 1000)
    ticker   = ticket["ticker"]
    resolved = _resolve_symbol(ticker)
    signal   = ticket["signal"]
    lots     = float(ticket["lots"])
    entry    = float(ticket["entry"])
    sl       = float(ticket["sl"])
    tp       = float(ticket["tp"])

    order_type = mt5.ORDER_TYPE_BUY if signal == "LONG" else mt5.ORDER_TYPE_SELL
    filling    = _get_filling_mode(resolved)

    # Fetch point once for slippage measurement
    with _mt5_lock:
        _sym = mt5.symbol_info(resolved)
    point = _sym.point if _sym else 0.0001

    _ensure_connected()

    for attempt in range(1, MAX_RETRIES + 1):
        with _mt5_lock:
            tick = mt5.symbol_info_tick(resolved)
        if tick is None:
            logger.error("symbol_info_tick returned None for %s — aborting", resolved)
            return

        price = tick.ask if signal == "LONG" else tick.bid

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

        sent_ms = int(time.time() * 1000)
        with _mt5_lock:
            result = mt5.order_send(request)
        fill_ms = int(time.time() * 1000)

        if result is None:
            logger.error("order_send returned None — %s", mt5.last_error())
            return

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            slippage_pips = abs(result.price - entry) / point if point > 0 else 0
            logger.info(
                "FILLED  %s %s→%s  %.2f lots | order=%d  price=%.5f  "
                "receipt→sent=%dms  sent→fill=%dms  slippage=%.1f ticks",
                signal, ticker, resolved, lots, result.order, result.price,
                sent_ms - receipt_ms, fill_ms - sent_ms, slippage_pips,
            )
            return

        retriable = (
            mt5.TRADE_RETCODE_REQUOTE,
            mt5.TRADE_RETCODE_PRICE_CHANGED,
            mt5.TRADE_RETCODE_PRICE_OFF,
        )
        if result.retcode in retriable:
            logger.warning("Retriable error %d on %s %s (attempt %d/%d) — retrying in %.1fs",
                           result.retcode, signal, ticker, attempt, MAX_RETRIES, RETRY_DELAY)
            time.sleep(RETRY_DELAY)
            continue

        logger.error("Order rejected — retcode=%d  %s | %s %s %.2f lots",
                     result.retcode, result.comment, signal, ticker, lots)
        return

    logger.error("Gave up after %d attempts — %s %s %.2f lots", MAX_RETRIES, signal, ticker, lots)


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


# ── REP thread — equity query responder ───────────────────────────────────

def _build_equity_reply(ticker: str) -> dict:
    try:
        with _mt5_lock:
            acct = mt5.account_info()
        balance = acct.balance if acct else 0.0
        equity  = acct.equity  if acct else 0.0

        point = contract_size = tick_value = 0.0
        digits = 5
        if ticker:
            try:
                point, contract_size, tick_value, digits = _contract_info(ticker)
            except Exception as exc:
                logger.warning("contract_info failed for %s: %s", ticker, exc)

        return {
            "balance":          balance,
            "equity":           equity,
            "point":            point,
            "contract_size":    contract_size,
            "trade_tick_value": tick_value,
            "digits":           digits,
        }
    except Exception as exc:
        logger.error("equity reply error: %s", exc)
        return {
            "error": str(exc),
            "balance": 0.0, "equity": 0.0,
            "point": 0.0, "contract_size": 0.0,
            "trade_tick_value": 0.0, "digits": 5,
        }


def _rep_loop(ctx: zmq.Context) -> None:
    sock = ctx.socket(zmq.REP)
    sock.bind(REP_ADDR)
    logger.info("REP socket bound on %s", REP_ADDR)

    while True:
        try:
            raw = sock.recv()
        except Exception as exc:
            logger.error("REP recv failed: %s — reopening socket", exc)
            sock.close()
            sock = ctx.socket(zmq.REP)
            sock.bind(REP_ADDR)
            continue

        try:
            msg    = json.loads(raw)
            ticker = msg.get("ticker", "EURUSD")
        except Exception:
            ticker = "EURUSD"

        reply = _build_equity_reply(ticker)
        try:
            sock.send_json(reply)
        except Exception as exc:
            logger.error("REP send failed: %s", exc)


# ── PULL loop — execution ticket receiver (main thread) ───────────────────

def _pull_loop(ctx: zmq.Context) -> None:
    sock = ctx.socket(zmq.PULL)
    sock.bind(PULL_ADDR)
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

            # SET_PARAMETERS — update static DD floor sent by Layer 2 on phase change
            if ticket.get("action") == "SET_PARAMETERS":
                floor = float(ticket.get("dd_floor", 0.0))
                with _dd_params_lock:
                    _dd_params["dd_floor"] = floor
                    _save_dd_params()
                logger.info("SET_PARAMETERS received: dd_floor=%.2f", floor)
                continue

            # Dormant guard — drop execution tickets during curfew / weekend
            with _dormant_lock:
                dormant = _dormant
            if dormant:
                logger.info("DORMANT — dropped ticket %s %s",
                            ticket.get("signal"), ticket.get("ticker"))
                continue

            logger.info("TICKET  %s %s  %.2f lots",
                        ticket.get("signal"), ticket.get("ticker"), ticket.get("lots"))
            _execute_order(ticket)

        except Exception as exc:
            logger.error("PULL loop error: %s", exc)


# ── Entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    _load_symbol_map()
    _load_dd_params()
    _connect_mt5()
    ctx = zmq.Context.instance()
    threading.Thread(target=_rep_loop,      args=(ctx,), daemon=True, name="rep-equity").start()
    threading.Thread(target=_sgt_scheduler, daemon=True, name="sgt-scheduler").start()
    if WORKER_NAME == "prop":
        threading.Thread(target=_static_dd_guard_loop, daemon=True, name="dd-guard").start()
        logger.info("Static DD guard started (prop worker only)")
    _pull_loop(ctx)  # blocks in main thread
