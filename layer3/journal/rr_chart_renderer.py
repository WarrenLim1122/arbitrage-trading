"""
Generate a consistent dark-theme risk/reward outcome chart from MT5 candle data.

Must be imported AFTER setting the Agg backend — this module sets it at import time.
All rendering is headless (no display required — safe for Windows Server VPS).
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Headless backend — must be set before pyplot import (Windows Server has no display)
import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SCREENSHOT_WIDTH  = int(os.getenv("SCREENSHOT_WIDTH",  "1600"))
SCREENSHOT_HEIGHT = int(os.getenv("SCREENSHOT_HEIGHT", "900"))

# Bars to show before entry and after close in the cropped view
_CTX_BARS  = 20   # context bars before entry
_AFT_BARS  = 2    # candles after close (just enough to show the close bar fully)
_LBL_SPACE = 14   # bar-width units reserved on the right for price labels

# ── Dark theme palette ────────────────────────────────────────────────────────
_BG    = "#0d1117"
_PANEL = "#161b22"
_TEXT  = "#e6edf3"
_GRID  = "#21262d"
_UP    = "#3fb950"   # bullish candle / TP / reward
_DOWN  = "#f85149"   # bearish candle / SL / risk
_ENTRY = "#58a6ff"   # entry line / marker
_CLOSE = "#ffa657"   # close-price marker
_WIN   = "#3fb950"
_LOSS  = "#f85149"
_BOX_A = 0.15        # box fill alpha


def _draw_candles(ax: plt.Axes, df: pd.DataFrame) -> None:
    """Draw OHLC candle bodies and wicks."""
    for i, row in enumerate(df.itertuples()):
        is_up   = row.close >= row.open
        color   = _UP if is_up else _DOWN
        body_lo = min(row.open, row.close)
        body_hi = max(row.open, row.close)
        height  = body_hi - body_lo or (row.high - row.low) * 0.001  # doji guard

        ax.bar(i, height, width=0.6, bottom=body_lo,
               color=color, edgecolor=color, linewidth=0.3, zorder=3)
        ax.vlines(i, row.low, row.high, color=color, linewidth=0.8, zorder=3)


def render_rr_chart(
    rates: np.ndarray,
    symbol: str,
    direction: str,        # "LONG" | "SHORT"
    entry_price: float,
    sl_price: float,
    tp_price: float,
    close_price: float,
    close_time: datetime,
    open_time: datetime,
    outcome: str,          # "WIN" | "LOSS"
    net_pnl: float,
    volume: float,
    ticket: int,
    account_type: str,
    close_reason: str,     # "TP" | "SL"
    rr_ratio: Optional[float] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Render chart and save to PNG.  Returns the output path.

    The visible window is cropped to 20 bars before entry → 2 bars after close
    so the trade fills the chart and the y-axis is tight around the trade range.
    """
    if output_path is None:
        tmp = Path(__file__).parent.parent.parent / "generated_screenshots"
        tmp.mkdir(exist_ok=True)
        output_path = tmp / f"{account_type}_{ticket}_outcome.png"

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.reset_index(drop=True)

    n = len(df)
    if n == 0:
        raise ValueError("Empty rates array — cannot render chart")

    def _utc(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    open_time_utc  = _utc(open_time)
    close_time_utc = _utc(close_time)

    times = df["time"].values  # numpy datetime64 array
    open_idx  = max(0, np.searchsorted(
        times, np.datetime64(open_time_utc.replace(tzinfo=None)), side="left") - 1)
    close_idx = min(n - 1, np.searchsorted(
        times, np.datetime64(close_time_utc.replace(tzinfo=None)), side="left"))

    # ── Crop the visible window ────────────────────────────────────────────────
    # Show only the trade horizon: context before entry + close + a couple bars after.
    view_start = max(0, open_idx - _CTX_BARS)
    view_end   = min(n - 1, close_idx + _AFT_BARS)

    # Y-axis: tight around the VISIBLE bars and key prices (not all 120 fetched bars)
    vis = df.iloc[view_start : view_end + 1]
    key_prices  = [entry_price, sl_price, tp_price, close_price,
                   float(vis["high"].max()), float(vis["low"].min())]
    price_range = max(key_prices) - min(key_prices) or abs(entry_price) * 0.01
    pad   = price_range * 0.15
    y_min = min(key_prices) - pad
    y_max = max(key_prices) + pad

    # ── Figure ────────────────────────────────────────────────────────────────
    dpi = 100
    fig, ax = plt.subplots(
        figsize=(SCREENSHOT_WIDTH / dpi, SCREENSHOT_HEIGHT / dpi),
        dpi=dpi, facecolor=_BG,
    )
    ax.set_facecolor(_PANEL)

    # Draw only candles up to view_end — no post-close bars visible
    _draw_candles(ax, df.iloc[: view_end + 1])

    # ── Risk / Reward boxes ────────────────────────────────────────────────────
    bx0 = open_idx  - 0.3   # left edge of entry bar body (bar width = 0.6, ±0.3 from centre)
    bx1 = close_idx + 0.3   # right edge of close bar body
    bw  = max(bx1 - bx0, 1.2)  # minimum 1.2 bars for very short trades

    if direction == "LONG":
        risk_lo, risk_h     = sl_price,    entry_price - sl_price
        reward_lo, reward_h = entry_price, tp_price    - entry_price
    else:
        risk_lo, risk_h     = entry_price, sl_price    - entry_price
        reward_lo, reward_h = tp_price,    entry_price - tp_price

    ax.add_patch(mpatches.Rectangle(
        (bx0, risk_lo), bw, risk_h,
        linewidth=1, edgecolor=_DOWN, facecolor=_DOWN, alpha=_BOX_A, zorder=2,
    ))
    ax.add_patch(mpatches.Rectangle(
        (bx0, reward_lo), bw, reward_h,
        linewidth=1, edgecolor=_UP, facecolor=_UP, alpha=_BOX_A, zorder=2,
    ))

    # ── Horizontal price lines (clipped to xlim automatically) ────────────────
    ax.axhline(entry_price, color=_ENTRY, linewidth=1.5, linestyle="--", alpha=0.9, zorder=4)
    ax.axhline(sl_price,    color=_DOWN,  linewidth=1.2, linestyle="--", alpha=0.75, zorder=4)
    ax.axhline(tp_price,    color=_UP,    linewidth=1.2, linestyle="--", alpha=0.75, zorder=4)

    # Close price: horizontal line from close bar to label column only
    label_x = view_end + 0.8
    ax.hlines(close_price, close_idx, label_x,
              colors=_CLOSE, linewidth=1.5, linestyles="-", zorder=4)
    ax.scatter([close_idx], [close_price], color=_CLOSE, s=80, zorder=6)

    # ── Entry triangle + direction label ──────────────────────────────────────
    entry_bar_low  = float(df.iloc[open_idx]["low"])
    entry_bar_high = float(df.iloc[open_idx]["high"])
    tri_gap = price_range * 0.025  # small gap between candle and marker

    if direction == "LONG":
        tri_y = entry_bar_low - tri_gap
        ax.scatter([open_idx], [tri_y], marker="^", color=_UP, s=200, zorder=7)
        ax.text(open_idx, tri_y - tri_gap, "LONG",
                color=_UP, fontsize=8, fontweight="bold",
                ha="center", va="top", zorder=8)
    else:
        tri_y = entry_bar_high + tri_gap
        ax.scatter([open_idx], [tri_y], marker="v", color=_DOWN, s=200, zorder=7)
        ax.text(open_idx, tri_y + tri_gap, "SHORT",
                color=_DOWN, fontsize=8, fontweight="bold",
                ha="center", va="bottom", zorder=8)

    # ── Vertical entry / close markers ────────────────────────────────────────
    ax.axvline(open_idx,  color=_ENTRY, linewidth=0.8, linestyle=":", alpha=0.5, zorder=3)
    ax.axvline(close_idx, color=_CLOSE, linewidth=0.8, linestyle=":", alpha=0.5, zorder=3)

    # ── Price labels — auto push-apart so they never overlap ─────────────────
    # Sort top → bottom by nominal price, then shift any label that is too close
    # to the one above it. A thin connector line links label to its true price.
    MIN_LABEL_GAP = price_range * 0.045   # ~4.5% of visible range
    raw = sorted([
        (tp_price,    _UP,    f"TP     {tp_price}"),
        (entry_price, _ENTRY, f"Entry {entry_price}"),
        (close_price, _CLOSE, f"Close {close_price}"),
        (sl_price,    _DOWN,  f"SL     {sl_price}"),
    ], key=lambda x: x[0], reverse=True)  # descending

    adj_ys = [raw[0][0]]
    for i in range(1, len(raw)):
        gap_needed = adj_ys[-1] - MIN_LABEL_GAP
        adj_ys.append(min(raw[i][0], gap_needed))

    for (nom_y, color, text), adj_y in zip(raw, adj_ys):
        ax.text(label_x + 0.3, adj_y, text, color=color, fontsize=8,
                va="center", ha="left", fontfamily="monospace", clip_on=False)
        if abs(adj_y - nom_y) > price_range * 0.002:
            # Small connector from actual price level to displaced label
            ax.plot([label_x, label_x + 0.25], [nom_y, adj_y],
                    color=color, linewidth=0.6, alpha=0.6, clip_on=False)

    # ── Outcome badge (top-right) ─────────────────────────────────────────────
    o_color  = _WIN if outcome == "WIN" else _LOSS
    pnl_sign = "+" if net_pnl >= 0 else ""
    rr_text  = f"   RR {rr_ratio:.2f}R" if rr_ratio else ""
    ax.text(
        0.98, 0.97,
        f"{outcome}  ${pnl_sign}{net_pnl:.2f}{rr_text}",
        transform=ax.transAxes, color=o_color,
        fontsize=14, fontweight="bold", va="top", ha="right",
        bbox=dict(facecolor=_PANEL, edgecolor=o_color,
                  boxstyle="round,pad=0.4", alpha=0.92),
    )

    # ── Direction badge (top-left) ────────────────────────────────────────────
    d_color = _UP if direction == "LONG" else _DOWN
    ax.text(
        0.02, 0.97,
        f"{direction}   {symbol}   {volume} lots   #{ticket}",
        transform=ax.transAxes, color=d_color,
        fontsize=11, fontweight="bold", va="top", ha="left",
        bbox=dict(facecolor=_PANEL, edgecolor=d_color,
                  boxstyle="round,pad=0.4", alpha=0.92),
    )

    # ── Meta label (bottom-left) ──────────────────────────────────────────────
    ax.text(
        0.02, 0.03,
        f"{account_type.upper()} • {close_reason} • "
        f"{close_time_utc.strftime('%Y-%m-%d %H:%M UTC')}",
        transform=ax.transAxes, color=_TEXT,
        fontsize=8, va="bottom", ha="left", alpha=0.65,
    )

    # ── Axis limits and ticks ─────────────────────────────────────────────────
    ax.set_xlim(view_start - 0.5, view_end + _LBL_SPACE)
    ax.set_ylim(y_min, y_max)

    visible_bars = view_end - view_start + 1
    tick_step = max(1, visible_bars // 6)
    tick_idx  = list(range(view_start, view_end + 1, tick_step))
    ax.set_xticks(tick_idx)
    ax.set_xticklabels(
        [df.iloc[i]["time"].strftime("%m-%d %H:%M") for i in tick_idx],
        rotation=30, ha="right", fontsize=8, color=_TEXT,
    )
    ax.tick_params(axis="y", colors=_TEXT, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(_GRID)
    ax.grid(True, color=_GRID, linewidth=0.5, alpha=0.6, zorder=0)

    fig.tight_layout(pad=1.5)
    fig.savefig(str(output_path), dpi=dpi, bbox_inches="tight",
                facecolor=_BG, edgecolor="none")
    plt.close(fig)

    logger.info("Chart saved → %s", output_path)
    return output_path
