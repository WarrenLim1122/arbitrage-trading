"""
Visual demo for rr_chart_renderer — renders a realistic trade with a +3h
MT5-server offset so marker placement can be eyeballed.

Run:  uv run --with matplotlib --with pandas python scripts/dev-tests/demo_chart_aesthetics.py
Output: logs/demo_chart_*.png
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

SERVER_OFFSET_H = 3.0  # MT5 server clock = UTC + 3


def make_rates(entry_idx: int, close_idx: int, entry: float, tp: float, sl: float,
               n: int = 150, bar_minutes: int = 15, seed: int = 7) -> np.ndarray:
    """Random walk that hits entry at entry_idx then drifts to TP by close_idx (SHORT win)."""
    rng = np.random.default_rng(seed)
    base_server_ts = int((datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
                          + timedelta(hours=SERVER_OFFSET_H)).timestamp())
    closes = np.empty(n)
    closes[entry_idx] = entry
    # pre-entry: random walk backwards from entry
    for i in range(entry_idx - 1, -1, -1):
        closes[i] = closes[i + 1] * (1 - rng.normal(0, 0.0006))
    # entry → close: drift toward TP with noise
    span = close_idx - entry_idx
    for k, i in enumerate(range(entry_idx + 1, n)):
        if i <= close_idx:
            target = entry + (tp - entry) * ((k + 1) / span)
            closes[i] = target + rng.normal(0, abs(entry - tp) * 0.06)
        else:
            closes[i] = closes[i - 1] * (1 + rng.normal(0, 0.0004))
    closes[close_idx] = tp

    rows = []
    for i in range(n):
        o = closes[i - 1] if i else closes[i]
        c = closes[i]
        noise = abs(entry) * rng.uniform(0.0001, 0.0004)
        h = max(o, c) + rng.uniform(0, noise)
        lo = min(o, c) - rng.uniform(0, noise)
        rows.append((base_server_ts + i * bar_minutes * 60, o, h, lo, c, 100, 0, 0))

    dtype = np.dtype([
        ("time", np.int64), ("open", np.float64), ("high", np.float64),
        ("low", np.float64), ("close", np.float64),
        ("tick_volume", np.int64), ("spread", np.int32), ("real_volume", np.int64),
    ])
    return np.array(rows, dtype=dtype)


if __name__ == "__main__":
    from layer3.journal.rr_chart_renderer import render_rr_chart

    entry_idx, close_idx = 115, 138
    entry, sl, tp = 0.78649, 0.78703, 0.78465
    rates = make_rates(entry_idx, close_idx, entry, tp, sl)

    # open/close times in SERVER tz (same clock as the bar stamps), like MT5 hands out
    open_time  = datetime.fromtimestamp(int(rates[entry_idx]["time"]) + 60, tz=timezone.utc)
    close_time = datetime.fromtimestamp(int(rates[close_idx]["time"]) + 300, tz=timezone.utc)

    out = render_rr_chart(
        rates=rates, symbol="USDCHF", direction="SHORT",
        entry_price=entry, sl_price=sl, tp_price=tp,
        close_price=tp, close_time=close_time, open_time=open_time,
        outcome="WIN", net_pnl=187.43, volume=1.94, ticket=8762504366,
        account_type="prop", close_reason="TP", rr_ratio=3.41,
        output_path=ROOT / "logs" / "demo_chart_short_win.png",
        server_utc_offset_hours=SERVER_OFFSET_H,
        account_currency="USD",
    )
    print(f"saved: {out}")

    # Second render: personal-side SGD LOSS, short trade duration
    out2 = render_rr_chart(
        rates=rates, symbol="USDCHF", direction="LONG",
        entry_price=entry, sl_price=0.78550, tp_price=0.78900,
        close_price=0.78550,
        close_time=datetime.fromtimestamp(int(rates[121]["time"]) + 300, tz=timezone.utc),
        open_time=open_time,
        outcome="LOSS", net_pnl=-42.17, volume=0.39, ticket=8762504367,
        account_type="personal", close_reason="SL", rr_ratio=None,
        output_path=ROOT / "logs" / "demo_chart_long_loss.png",
        server_utc_offset_hours=SERVER_OFFSET_H,
        account_currency="SGD",
    )
    print(f"saved: {out2}")
