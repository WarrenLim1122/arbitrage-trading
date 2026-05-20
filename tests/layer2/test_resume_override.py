"""Regression tests for the /resume soft-kill override and dynamic session-time text.

Bug context:
- Before this fix, /resume cleared `daily_halted` and `active=False`, but the
  monitor's next tick immediately re-fired the same kill (e.g. K3 cap still
  breached), silently undoing the resume.
- Several user-facing alerts hardcoded "12:00 SGT" / "00:00–12:00", which
  remained wrong when the user changed /setwindow.

These tests exercise the persistence layer and the dynamic helpers directly,
so they need none of the FastAPI / ZMQ runtime.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from layer2 import state


SGT = ZoneInfo("Asia/Singapore")


def _seed_phase(tmp_path, monkeypatch, **overrides):
    pc = tmp_path / "phase_config.json"
    base = {"phase": 1, "active": True}
    base.update(overrides)
    pc.write_text(json.dumps(base))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)
    return pc


def test_save_phase_persists_soft_kill_override_day(tmp_path, monkeypatch):
    """/resume writes soft_kill_override_day; _save_phase keeps it as a top-level key."""
    pc = _seed_phase(tmp_path, monkeypatch)

    state._save_phase({"phase": 1, "active": True, "soft_kill_override_day": "2026-05-20"})

    on_disk = json.loads(pc.read_text())
    assert on_disk["soft_kill_override_day"] == "2026-05-20"


def test_save_phase_can_clear_soft_kill_override_day(tmp_path, monkeypatch):
    """Auto-resume at session rollover pops the override — _save_phase must honour it."""
    pc = _seed_phase(tmp_path, monkeypatch, soft_kill_override_day="2026-05-19")

    state._save_phase({"phase": 1, "active": True})

    on_disk = json.loads(pc.read_text())
    assert "soft_kill_override_day" not in on_disk


def test_propfirm_day_boundary_at_11sgt():
    """Override day uses _propfirm_day, which rolls at 11:00 SGT.

    A /resume at 10:59 SGT belongs to *yesterday's* prop day; the next monitor
    tick at 11:01 SGT will see today's prop day and the override naturally
    expires. This is the safety property that makes the override self-cleaning.
    """
    before_roll = datetime(2026, 5, 20, 10, 59, tzinfo=SGT)
    after_roll  = datetime(2026, 5, 20, 11, 1, tzinfo=SGT)
    assert state._propfirm_day(before_roll) == "2026-05-19"
    assert state._propfirm_day(after_roll)  == "2026-05-20"


def test_window_24h_means_no_curfew_on_weekday(tmp_path, monkeypatch):
    """When user sets /setwindow 00:00 00:00, weekdays have no curfew.

    Verifies _is_sgt_curfew respects the live trading_window — the fix to make
    halt messages dynamic depends on this contract holding.
    """
    monkeypatch.setattr(state, "TRADING_WINDOW_PATH", tmp_path / "trading_window.json")
    # weekday Monday 2026-05-18 03:00 SGT
    weekday_dawn = datetime(2026, 5, 18, 3, 0, tzinfo=SGT)

    # Original 12:00–00:00 window — 03:00 is curfew
    state._trading_window["current_window"] = {"start": "12:00", "end": "00:00"}
    assert state._is_sgt_curfew(weekday_dawn) is True

    # 24-hour window — same moment is NOT curfew
    state._trading_window["current_window"] = {"start": "00:00", "end": "00:00"}
    assert state._is_sgt_curfew(weekday_dawn) is False

    # Restore default so other tests aren't affected
    state._trading_window["current_window"] = {"start": "12:00", "end": "00:00"}


def test_window_minutes_treats_zero_end_as_eod():
    """'00:00' as an end-of-day sentinel must read as 1440, not 0 — otherwise the
    24-hour window 00:00–00:00 would degenerate into 'always closed'."""
    assert state._window_minutes("00:00", is_end=False) == 0      # midnight start
    assert state._window_minutes("00:00", is_end=True)  == 1440   # end-of-day
    assert state._window_minutes("12:00", is_end=False) == 720
    assert state._window_minutes("12:30", is_end=True)  == 750
