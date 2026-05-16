import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import zmq

logger = logging.getLogger("layer2")

# ── Timezone ──────────────────────────────────────────────────────────────
SGT = ZoneInfo("Asia/Singapore")

# ── Paths ─────────────────────────────────────────────────────────────────
ROOT                   = Path(__file__).parent.parent
PHASE_CONFIG_PATH      = ROOT / "config" / "phase_config.json"
RISK_PARAMS_PATH       = ROOT / "config" / "risk_params.json"
PROPFIRM_CONFIG_PATH   = ROOT / "config" / "propfirm_config.json"
CONSISTENCY_LOG_PATH   = ROOT / "config" / "consistency_log.json"
SYMBOL_MAP_PATH        = ROOT / "config" / "symbol_map.json"
TRADING_WINDOW_PATH    = ROOT / "config" / "trading_window.json"

# ── Env vars ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = int(os.environ["TELEGRAM_CHAT_ID"])

# ── Risk params ───────────────────────────────────────────────────────────
with RISK_PARAMS_PATH.open() as _f:
    _risk = json.load(_f)

# ── Symbol map (canonical ticker → broker MT5 symbol) ─────────────────────
try:
    with SYMBOL_MAP_PATH.open() as _f:
        _SYMBOL_MAP: dict[str, str] = json.load(_f)
except Exception:
    _SYMBOL_MAP = {}

PROP_RISK_PCT  = float(_risk["prop_risk_pct"])
PHASE_MULT     = {int(k): float(v) for k, v in _risk["phase_multipliers"].items()}
ZMQ_PUSH_PROP  = _risk["layer3_zmq"]["prop"]["push"]
ZMQ_PUSH_PERS  = _risk["layer3_zmq"]["personal"]["push"]
ZMQ_REQ_PROP   = _risk["layer3_zmq"]["prop"]["rep"]
ZMQ_REQ_PERS   = _risk["layer3_zmq"]["personal"]["rep"]
EQUITY_TIMEOUT = 3_000  # ms

# ── Allowed pairs + news-filter currencies — loaded from config/allowed_pairs.json ──
# To add or remove a pair, edit that file and restart Layer 2. No code change needed.
# Format: { "EURUSD": ["EUR", "USD"], ... }
_ALLOWED_PAIRS_PATH = ROOT / "config" / "allowed_pairs.json"
with _ALLOWED_PAIRS_PATH.open() as _f:
    _pair_config: dict[str, list[str]] = json.load(_f)

ALLOWED_PAIRS: frozenset[str] = frozenset(_pair_config.keys())

# ForexFactory currency codes that each pair is sensitive to.
# FF tags events with currency codes directly (e.g. "USD", "EUR") — no country mapping needed.
_TICKER_CURRENCIES: dict[str, frozenset[str]] = {
    ticker: frozenset(currencies)
    for ticker, currencies in _pair_config.items()
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

# ── Equity monitor globals ─────────────────────────────────────────────────
_last_curfew_close_date: date | None = None

_prop_fail_count:      int  = 0
_pers_fail_count:      int  = 0
_prop_down:            bool = False
_pers_down:            bool = False
_WORKER_DOWN_THRESHOLD: int = 3   # consecutive 30 s misses before alert (~90 s)
_prop_algo_disabled:   bool = False   # True while prop MT5 reports trade_allowed=False
_pers_algo_disabled:   bool = False   # True while personal MT5 reports trade_allowed=False


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
    _consistency_log.clear()
    if CONSISTENCY_LOG_PATH.exists():
        with CONSISTENCY_LOG_PATH.open() as f:
            _consistency_log.update(json.load(f))
    else:
        _consistency_log["days"] = []


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


def _invert(signal: str) -> str:
    return "SHORT" if signal == "LONG" else "LONG"


# ── Buffer logic ──────────────────────────────────────────────────────────

def _apply_buffers(raw: dict) -> dict:
    """Apply safety buffers to raw prop firm limits.

    - Daily DD: subtract 0.5 percentage point (buffer against prop firm's daily limit).
    - Overall DD: NO buffer — trigger at exact value user inputs (prop firm closes at this exact %).
    - Daily profit cap: enforce at 25% of target (vs the 30% consistency rule).
    - Consistency threshold: subtract 1 percentage point (fire 1% before the firm's stated limit).
    """
    effective = raw.copy()
    effective["max_drawdown_daily_pct"]      = round(raw["max_drawdown_daily_pct"]          - 0.5, 2)
    effective["max_drawdown_overall_pct"]    = raw["max_drawdown_overall_pct"]
    effective["daily_profit_cap_pct"]        = round(raw["profit_target_pct"] * 0.25, 2)
    effective["consistency_threshold_pct"]   = round(raw.get("consistency_threshold_pct", 30.0) - 1.0, 2)
    return effective


# ── Trading window config ─────────────────────────────────────────────────

_trading_window: dict = {
    "current_window": {"start": "12:00", "end": "00:00"},
    "next_window": None,
}
_window_lock = threading.Lock()


def _load_trading_window() -> None:
    if TRADING_WINDOW_PATH.exists():
        with TRADING_WINDOW_PATH.open() as f:
            data = json.load(f)
        with _window_lock:
            _trading_window.update(data)
    else:
        _save_trading_window()


def _save_trading_window() -> None:
    TRADING_WINDOW_PATH.parent.mkdir(exist_ok=True)
    with TRADING_WINDOW_PATH.open("w") as f:
        json.dump(_trading_window, f, indent=2)


def _apply_next_window() -> dict | None:
    """Swap next_window → current_window if one is scheduled. Returns the new window or None."""
    with _window_lock:
        if _trading_window.get("next_window"):
            _trading_window["current_window"] = _trading_window["next_window"]
            _trading_window["next_window"] = None
            _save_trading_window()
            return dict(_trading_window["current_window"])
    return None


def _window_minutes(t_str: str, is_end: bool = False) -> int:
    """Convert 'HH:MM' to minutes since midnight.

    '00:00' as an end time means end-of-day (1440). '00:00' as a start time
    means midnight (0), enabling 24-hour weekday trading when start=end='00:00'.
    """
    h, m = map(int, t_str.split(":"))
    mins = h * 60 + m
    return 1440 if (mins == 0 and is_end) else mins


_load_trading_window()

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


def _is_sgt_curfew(now_sgt: datetime | None = None) -> bool:
    if now_sgt is None:
        now_sgt = _sgt_now()
    if now_sgt.weekday() >= 5:
        return True
    with _window_lock:
        window = dict(_trading_window["current_window"])
    start = _window_minutes(window.get("start", "12:00"), is_end=False)
    end   = _window_minutes(window.get("end",   "00:00"), is_end=True)
    curr  = now_sgt.hour * 60 + now_sgt.minute
    return curr < start or curr >= end


def _pnl_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(int(round(pct / 100 * width)), width))
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:.1f}%"


def _fmt_price(symbol: str, price: float) -> str:
    sym = symbol.upper()
    if "JPY" in sym:
        digits = 3
    elif sym == "XAUUSD":
        digits = 2
    elif sym == "XAGUSD":
        digits = 4
    else:
        digits = 5
    return f"{price:.{digits}f}"


# ── Phase 2 field definitions (used by telegram_handlers wizard) ───────────

# Ordered field definitions used to display and collect Phase 2 settings.
# Each entry: (1-based index, config_key, display_name, input_type)
# propfirm_name removed — stored silently as "Prop Account"; never shown to user.
_P2_FIELD_DEFS = [
    (1, "profit_target_pct",         "Profit target %",            "float_pos"),
    (2, "max_drawdown_overall_pct",  "Max DD overall %",           "float_pos"),
    (3, "max_drawdown_daily_pct",    "Max DD daily %",             "float_pos"),
    (4, "drawdown_is_static",        "Drawdown type",              "static_dynamic"),
    (5, "raw_spread_account",        "Raw spread account",         "yes_no"),
    (6, "profit_sharing_pct",        "Profit sharing %",           "float_pos"),
    (7, "min_profit_days",           "Min profit days",            "int_nn"),
    (8, "consistency_threshold_pct", "Consistency threshold %",    "float_pos"),
]
_P2_FIELD_BY_IDX: dict[int, tuple] = {d[0]: d for d in _P2_FIELD_DEFS}


def _p2_display(key: str, value) -> str:
    if key == "drawdown_is_static":
        return "Static" if value else "Dynamic"
    if key == "raw_spread_account":
        return "Yes" if value else "No"
    if key == "max_drawdown_daily_pct":
        return f"{value:.1f}% (enforced at {value - 0.5:.1f}% after −0.5pp buffer)"
    if key in ("profit_target_pct", "max_drawdown_overall_pct",
               "profit_sharing_pct", "consistency_threshold_pct"):
        return f"{value:.1f}%"
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
