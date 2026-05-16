# Phase 1 Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split Layer 2 phase logic into isolated strategy modules and add the new Phase 1 dynamic reward-targeting strategy, leaving Phase 2 byte-identical.

**Architecture:** Two new *pure* modules (`phase1_strategy.py`, `phase2_strategy.py`) plus a shared pure helper (`strategy_common.py`). `logic_core.py` stays the orchestrator and dispatches geometry + kills by `_phase_state["phase"]`. Phase 2's module is a verbatim extraction guarded by a regression test; all new risk lives in Phase 1's module. State for the ratchet persists in a `phase1` block inside `phase_config.json`.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, FastAPI, python-telegram-bot, pyzmq. Spec: `docs/superpowers/specs/2026-05-16-phase1-strategy-design.md`.

**Design rules:** Strategy modules import **nothing** from `layer2.state` or `layer2.logic_core` (those read env vars / spawn threads on import). They take primitives and return dicts → unit-testable with zero env setup. The orchestrator owns all I/O, persistence, ZMQ, Telegram.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `tests/conftest.py` | Set dummy env vars + sys.path so `layer2.state` is importable in tests | Create |
| `tests/layer2/test_strategy_common.py` | Pure helper tests | Create |
| `tests/layer2/test_phase1_strategy.py` | Phase 1 parse/validate/stages/ratchet/geometry/kills | Create |
| `tests/layer2/test_phase2_strategy.py` | Phase 2 extraction regression (byte-identical) | Create |
| `tests/layer2/test_buffers.py` | `_apply_buffers` −0.5pp | Create |
| `layer2/strategy_common.py` | `invert_signal()`, `dollar_per_unit()` — pure, shared | Create |
| `layer2/phase2_strategy.py` | `compute_geometry()` — verbatim Phase 2 math | Create |
| `layer2/phase1_strategy.py` | parse/validate/derive_stages/active_stage_index/compute_geometry/evaluate_kills | Create |
| `layer2/state.py` | −0.5pp buffer; `_p2_display` text; phase1-block load/save/init/ratchet helpers | Modify |
| `layer2/logic_core.py` | Dispatch geometry (`receive_signal`) + kills (`_run_equity_check`) by phase; Phase 1 news skip | Modify |
| `layer2/telegram_handlers.py` | `/phase1` ConversationHandler wizard; register; remove old plain handler | Modify |
| `pyproject.toml` | Add `[tool.pytest.ini_options]` | Modify |
| `docs/superpowers/specs/2026-05-16-phase1-strategy-design.md` | (reference only) | — |

---

## Task 1: Test scaffolding

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/__init__.py`, `tests/layer2/__init__.py`
- Modify: `pyproject.toml` (end of file)

- [ ] **Step 1: Create test package markers**

```bash
mkdir -p tests/layer2
touch tests/__init__.py tests/layer2/__init__.py
```

- [ ] **Step 2: Create `tests/conftest.py`**

```python
"""Shared pytest setup.

Strategy modules are pure and need none of this. It exists only so tests that
import layer2.state (which reads TELEGRAM_* env vars at import time) do not
crash during collection.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 3: Append pytest config to `pyproject.toml`**

Append to the end of `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Verify collection works**

Run: `uv run --extra dev pytest tests/ -q`
Expected: `no tests ran` (exit code 5) — collection succeeds, zero tests.

- [ ] **Step 5: Commit**

```bash
git add tests/ pyproject.toml
git commit -m "Add pytest scaffolding for Layer 2 strategy tests"
```

---

## Task 2: `strategy_common.py` — pure shared helpers

**Files:**
- Create: `layer2/strategy_common.py`
- Test: `tests/layer2/test_strategy_common.py`

- [ ] **Step 1: Write the failing test**

`tests/layer2/test_strategy_common.py`:

```python
from layer2.strategy_common import invert_signal, dollar_per_unit


def test_invert_signal():
    assert invert_signal("LONG") == "SHORT"
    assert invert_signal("SHORT") == "LONG"


def test_dollar_per_unit_usd_quote_uses_contract_size():
    # EURUSD ends in USD and has a contract size → k = contract_size
    k = dollar_per_unit("EURUSD", contract_size=100000.0,
                         tick_size=0.00001, tick_value=1.0)
    assert k == 100000.0


def test_dollar_per_unit_usd_base_uses_tick_ratio():
    # USDJPY does NOT end in USD → k = tick_value / tick_size
    k = dollar_per_unit("USDJPY", contract_size=100000.0,
                         tick_size=0.001, tick_value=0.65)
    assert k == 0.65 / 0.001


def test_dollar_per_unit_zero_contract_falls_back_to_tick_ratio():
    k = dollar_per_unit("EURUSD", contract_size=0.0,
                        tick_size=0.00001, tick_value=1.0)
    assert k == 1.0 / 0.00001
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/layer2/test_strategy_common.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'layer2.strategy_common'`

- [ ] **Step 3: Create `layer2/strategy_common.py`**

```python
"""Pure helpers shared by phase strategy modules.

No imports from layer2.state / layer2.logic_core — keep this side-effect free
and unit-testable.
"""


def invert_signal(signal: str) -> str:
    """LONG <-> SHORT."""
    return "SHORT" if signal == "LONG" else "LONG"


def dollar_per_unit(
    ticker: str,
    contract_size: float,
    tick_size: float,
    tick_value: float,
) -> float:
    """Return k such that dollar_per_lot at price-distance X == X * k.

    Mirrors the existing Layer 2 rule (logic_core.receive_signal):
      - xxxUSD pair with a contract size → P&L is USD/unit → k = contract_size
        (dollar_per_lot = distance * contract_size)
      - otherwise → use broker tick data → k = tick_value / tick_size
        (dollar_per_lot = (distance / tick_size) * tick_value)
    """
    if ticker.endswith("USD") and contract_size > 0:
        return contract_size
    return tick_value / tick_size
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/layer2/test_strategy_common.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add layer2/strategy_common.py tests/layer2/test_strategy_common.py
git commit -m "Add pure strategy_common helpers (invert_signal, dollar_per_unit)"
```

---

## Task 3: `phase1_strategy.py` — input parse, validation, stage derivation, ratchet

**Files:**
- Create: `layer2/phase1_strategy.py`
- Test: `tests/layer2/test_phase1_strategy.py`

- [ ] **Step 1: Write the failing test**

`tests/layer2/test_phase1_strategy.py`:

```python
import pytest

from layer2.phase1_strategy import (
    parse_reward_risk,
    validate_phase1_inputs,
    derive_stages,
    active_stage_index,
)


def test_parse_reward_risk_ok():
    assert parse_reward_risk("9000:2000") == (9000.0, 2000.0)
    assert parse_reward_risk("  9000 : 2000 ") == (9000.0, 2000.0)
    assert parse_reward_risk("9000.5:2000") == (9000.5, 2000.0)


@pytest.mark.parametrize("bad", ["9000", "abc", "9000:0", "0:2000", "9000:-1", ":", "9000:2000:1"])
def test_parse_reward_risk_rejects(bad):
    with pytest.raises(ValueError):
        parse_reward_risk(bad)


def test_validate_ok():
    # baseline 100000, target 10% = 10000; W1 9000 < 10000; days 3
    assert validate_phase1_inputs(9000, 2000, 100000, 10.0, 3) is None


def test_validate_rejects_w1_ge_target():
    err = validate_phase1_inputs(10000, 2000, 100000, 10.0, 3)
    assert err is not None and "target" in err.lower()


def test_validate_rejects_min_days_lt_2():
    err = validate_phase1_inputs(9000, 2000, 100000, 10.0, 1)
    assert err is not None


def test_validate_rejects_nonpositive():
    assert validate_phase1_inputs(0, 2000, 100000, 10.0, 3) is not None
    assert validate_phase1_inputs(9000, 0, 100000, 10.0, 3) is not None
    assert validate_phase1_inputs(9000, 2000, 0, 10.0, 3) is not None


def test_derive_stages_three_days():
    assert derive_stages(100000, 9000, 10.0, 3) == [109000.0, 109500.0, 110000.0]


def test_derive_stages_four_days():
    s = derive_stages(100000, 9000, 10.0, 4)
    assert s[0] == 109000.0
    assert s[-1] == 110000.0
    assert len(s) == 4
    assert s[1] == pytest.approx(109333.33, abs=0.01)


def test_active_stage_index_start_and_ratchet():
    stages = [109000.0, 109500.0, 110000.0]
    assert active_stage_index(stages, 100000.0, 0) == 0          # aiming 109000
    assert active_stage_index(stages, 109000.0, 0) == 1          # reached S1 -> aim 109500
    # never reverts after a loss
    assert active_stage_index(stages, 107000.0, 1) == 1          # still aim 109500
    assert active_stage_index(stages, 109500.0, 1) == 2          # reached S2 -> aim 110000
    assert active_stage_index(stages, 110000.0, 2) == 3          # final reached (== len)


def test_active_stage_index_skips_overshoot():
    stages = [109000.0, 109500.0, 110000.0]
    assert active_stage_index(stages, 109800.0, 0) == 2          # jumped past S1+S2


def test_active_stage_index_init_above_first():
    stages = [109000.0, 109500.0, 110000.0]
    assert active_stage_index(stages, 109200.0, 0) == 1          # start already above S1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_strategy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'layer2.phase1_strategy'`

- [ ] **Step 3: Create `layer2/phase1_strategy.py` with these functions**

```python
"""Phase 1 — dynamic reward-targeting strategy (pure).

No imports from layer2.state / layer2.logic_core. All inputs are primitives;
outputs are plain dicts. See docs/superpowers/specs/2026-05-16-phase1-strategy-design.md
"""
from __future__ import annotations

from layer2.strategy_common import invert_signal, dollar_per_unit


def parse_reward_risk(text: str) -> tuple[float, float]:
    """Parse a 'reward:risk' dollar pair, e.g. '9000:2000' -> (9000.0, 2000.0).

    Raises ValueError on any malformed / non-positive input.
    """
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError("expected exactly one ':' (format reward:risk)")
    try:
        reward = float(parts[0].strip())
        risk = float(parts[1].strip())
    except ValueError:
        raise ValueError("reward and risk must be numbers")
    if reward <= 0 or risk <= 0:
        raise ValueError("reward and risk must be positive")
    return reward, risk


def validate_phase1_inputs(
    first_reward: float,
    fixed_risk: float,
    baseline: float,
    profit_target_pct: float,
    min_profit_days: int,
) -> str | None:
    """Return an error string if inputs are unusable, else None."""
    if first_reward <= 0 or fixed_risk <= 0 or baseline <= 0:
        return "Reward, risk and baseline must all be positive."
    if profit_target_pct <= 0:
        return "Prop-firm profit target % is not set — run /changepropfirm first."
    if min_profit_days < 2:
        return ("Prop-firm min profitable days must be ≥ 2 "
                "(need Stage 1 + the funded line). Set it via /changepropfirm.")
    target = baseline * profit_target_pct / 100.0
    if first_reward >= target:
        return (f"First reward ${first_reward:,.0f} must be LESS than the overall "
                f"target ${target:,.0f} (else there is no room for later stages).")
    return None


def derive_stages(
    baseline: float,
    first_reward: float,
    profit_target_pct: float,
    min_profit_days: int,
) -> list[float]:
    """Cumulative absolute prop-equity targets.

    stages[0]  = baseline + first_reward
    stages[-1] = baseline + (baseline * profit_target_pct/100)   (funded line)
    Intermediate stages split (target - first_reward) evenly over (n-1) days.
    """
    target = baseline * profit_target_pct / 100.0
    n = int(min_profit_days)
    step = (target - first_reward) / (n - 1)
    return [round(baseline + first_reward + step * i, 2) for i in range(n)]


def active_stage_index(stages: list[float], current_equity: float, prev_index: int) -> int:
    """Index of the lowest stage strictly greater than current_equity.

    Ratchets only — never returns below prev_index. Returns len(stages) when the
    final stage has been reached (caller treats that as K4 / funded).
    """
    idx = max(0, int(prev_index))
    while idx < len(stages) and current_equity >= stages[idx]:
        idx += 1
    return idx
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_strategy.py -q`
Expected: PASS (all parametrized cases pass)

- [ ] **Step 5: Commit**

```bash
git add layer2/phase1_strategy.py tests/layer2/test_phase1_strategy.py
git commit -m "Add Phase 1 input parsing, validation, stage derivation and ratchet"
```

---

## Task 4: `phase1_strategy.compute_geometry()`

**Files:**
- Modify: `layer2/phase1_strategy.py`
- Test: `tests/layer2/test_phase1_strategy.py` (append)

- [ ] **Step 1: Append the failing test**

Append to `tests/layer2/test_phase1_strategy.py`:

```python
from layer2.phase1_strategy import compute_geometry

# EURUSD: ends in "USD", contract_size 100000 -> k = 100000
_BASE = dict(
    ticker="EURUSD", signal="LONG", entry=1.08500, signal_sl=1.08300,
    price_digits=5,
    prop_contract_size=100000.0, prop_tick_size=0.00001, prop_tick_value=1.0,
    pers_contract_size=100000.0, pers_tick_size=0.00001, pers_tick_value=1.0,
    fixed_risk=2000.0, pers_ratio=0.20,
)


def test_geometry_first_trade():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0, **_BASE)
    assert "reject" not in g
    # D = 0.00200 ; lots_prop = 9000 / (0.002*100000) = 45.0
    assert g["prop_lots"] == 45.0
    assert g["prop_signal"] == "SHORT"           # inverse of LONG
    assert g["pers_signal"] == "LONG"            # follows signal
    assert g["prop_tp"] == 1.08300               # prop TP == signal SL price
    assert g["pers_sl"] == 1.08300               # personal SL == signal SL price
    # prop SL distance = D * R / reward = 0.002*2000/9000 = 0.000444444
    assert g["prop_sl"] == round(1.08500 + 0.000444444, 5)   # 1.08544
    assert g["pers_tp"] == g["prop_sl"]          # shared anchor
    assert g["prop_dollar_risk"] == pytest.approx(2000.0, abs=0.01)
    assert g["prop_reward"] == pytest.approx(9000.0, abs=0.01)
    assert g["pers_lots"] == 9.0                 # 45 * 0.20
    assert g["pers_dollar_risk"] == pytest.approx(1800.0, abs=0.01)  # 0.2*9000
    assert g["pers_reward"] == pytest.approx(400.0, abs=0.01)        # 0.2*2000
    assert g["prop_rr"] == pytest.approx(4.5, abs=0.001)


def test_geometry_harder_after_losses():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=98000.0, **_BASE)
    assert g["prop_lots"] == 55.0                # 11000 / 200
    assert g["prop_dollar_risk"] == pytest.approx(2000.0, abs=0.01)
    assert g["prop_reward"] == pytest.approx(11000.0, abs=0.01)
    assert g["pers_dollar_risk"] == pytest.approx(2200.0, abs=0.01)  # 0.2*11000
    assert g["pers_reward"] == pytest.approx(400.0, abs=0.01)


def test_geometry_short_signal_mirrors():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "signal": "SHORT", "signal_sl": 1.08700})
    # SHORT signal: signal_sl above entry; D = 0.00200
    assert g["prop_signal"] == "LONG"
    assert g["pers_signal"] == "SHORT"
    assert g["prop_tp"] == 1.08700               # prop TP == signal SL price
    assert g["pers_sl"] == 1.08700
    assert g["prop_sl"] == round(1.08500 - 0.000444444, 5)
    assert g["pers_tp"] == g["prop_sl"]


def test_geometry_rejects_zero_distance():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          **{**_BASE, "signal_sl": 1.08500})
    assert "reject" in g


def test_geometry_rejects_nonpositive_reward():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=109000.0, **_BASE)
    assert "reject" in g


def test_geometry_rejects_lots_round_to_zero():
    # tiny reward gap + huge D -> lots < 0.005 -> rounds to 0.0
    g = compute_geometry(active_stage=100000.01, live_prop_equity=100000.0, **_BASE)
    assert "reject" in g


def test_geometry_rejects_over_max_lots():
    g = compute_geometry(active_stage=109000.0, live_prop_equity=100000.0,
                          max_prop_lots=10.0, **_BASE)
    assert "reject" in g and "max" in g["reject"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_strategy.py -q`
Expected: FAIL — `ImportError: cannot import name 'compute_geometry'`

- [ ] **Step 3: Append `compute_geometry` to `layer2/phase1_strategy.py`**

```python
def compute_geometry(
    *,
    ticker: str,
    signal: str,
    entry: float,
    signal_sl: float,
    price_digits: int,
    prop_contract_size: float,
    prop_tick_size: float,
    prop_tick_value: float,
    pers_contract_size: float,
    pers_tick_size: float,
    pers_tick_value: float,
    active_stage: float,
    live_prop_equity: float,
    fixed_risk: float,
    pers_ratio: float,
    max_prop_lots: float = 0.0,
) -> dict:
    """Phase 1 geometry.

    Anchor = signal SL price = personal SL = prop TP.
    Prop SL & personal TP are computed (shared mirror price).
    lots_personal = pers_ratio * lots_prop.

    Returns a dict of ticket fields, or {"reject": "<reason>"}.
    """
    reward_prop = round(active_stage - live_prop_equity, 2)
    if reward_prop <= 0:
        return {"reject": "equity at/above active stage — awaiting ratchet"}

    d = abs(entry - signal_sl)
    if d <= 0:
        return {"reject": f"signal SL distance is zero (entry={entry} sl={signal_sl})"}

    prop_k = dollar_per_unit(ticker, prop_contract_size, prop_tick_size, prop_tick_value)
    pers_k = dollar_per_unit(ticker, pers_contract_size, pers_tick_size, pers_tick_value)
    if prop_k <= 0 or pers_k <= 0:
        return {"reject": "invalid contract data (dollar-per-unit <= 0)"}

    # Prop TP anchored at the signal-SL distance D → this lot size makes the
    # prop win exactly the stage gap.
    lots_prop = round(reward_prop / (d * prop_k), 2)
    if lots_prop <= 0:
        return {"reject": "computed prop lots rounds to 0 (reward gap too small for SL distance)"}
    if max_prop_lots > 0 and lots_prop > max_prop_lots:
        return {"reject": f"computed prop lots {lots_prop:.2f} exceed max {max_prop_lots:.2f}"}

    # Prop SL distance sized so a prop loss = exactly fixed_risk.
    prop_sl_dist = fixed_risk / (lots_prop * prop_k)

    lots_pers = round(lots_prop * pers_ratio, 2)
    if lots_pers <= 0:
        return {"reject": "computed personal lots rounds to 0"}

    prop_signal = invert_signal(signal)
    if signal == "LONG":
        # prop SHORT: TP below entry (= signal SL), SL above entry
        prop_tp_price = entry - d
        prop_sl_price = entry + prop_sl_dist
    else:
        # signal SHORT -> prop LONG: TP above entry (= signal SL), SL below entry
        prop_tp_price = entry + d
        prop_sl_price = entry - prop_sl_dist

    prop_tp = round(prop_tp_price, price_digits)
    prop_sl = round(prop_sl_price, price_digits)
    pers_sl = round(signal_sl, price_digits)   # personal SL == signal SL price
    pers_tp = prop_sl                          # personal TP == prop SL price (shared)

    # Dollar figures from UNROUNDED distances (display/alert only).
    prop_dollar_risk = round(lots_prop * prop_k * prop_sl_dist, 2)
    prop_reward = round(lots_prop * prop_k * d, 2)
    pers_dollar_risk = round(lots_pers * pers_k * d, 2)
    pers_reward = round(lots_pers * pers_k * prop_sl_dist, 2)
    prop_rr = prop_reward / prop_dollar_risk if prop_dollar_risk > 0 else 0.0
    pers_rr = pers_reward / pers_dollar_risk if pers_dollar_risk > 0 else 0.0

    return {
        "prop_signal": prop_signal,
        "prop_lots": lots_prop,
        "prop_sl": prop_sl,
        "prop_tp": prop_tp,
        "prop_dollar_risk": prop_dollar_risk,
        "prop_reward": prop_reward,
        "prop_rr": round(prop_rr, 4),
        "pers_signal": signal,
        "pers_lots": lots_pers,
        "pers_sl": pers_sl,
        "pers_tp": pers_tp,
        "pers_dollar_risk": pers_dollar_risk,
        "pers_reward": pers_reward,
        "pers_rr": round(pers_rr, 4),
        "sl_distance": round(d, price_digits),
        "tp_distance": round(prop_sl_dist, price_digits),
        "active_stage": active_stage,
        "reward_gap": reward_prop,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_strategy.py -q`
Expected: PASS (all geometry tests pass)

- [ ] **Step 5: Commit**

```bash
git add layer2/phase1_strategy.py tests/layer2/test_phase1_strategy.py
git commit -m "Add Phase 1 compute_geometry with signal-SL-anchored mirror"
```

---

## Task 5: `phase2_strategy.compute_geometry()` — verbatim extraction + regression

**Files:**
- Create: `layer2/phase2_strategy.py`
- Test: `tests/layer2/test_phase2_strategy.py`

The body is the exact math from `logic_core.py` lines 1418–1511, parameterised. The regression test pins it to numbers computed by hand from the *current* formula.

- [ ] **Step 1: Write the failing regression test**

`tests/layer2/test_phase2_strategy.py`:

```python
import pytest

from layer2.phase2_strategy import compute_geometry

# Reproduces the CURRENT logic_core math for a known signal so the extraction
# is provably byte-identical. EURUSD ends in "USD", contract_size 100000.
#   prop_dollar_risk = baseline * 0.0067 = 100000 * 0.0067 = 670.0
#   sl_distance = |entry - sl| = |1.08500 - 1.08300| = 0.00200
#   tp_distance = |tp - entry| = |1.08554 - 1.08500| = 0.00054
#   prop_dollar_per_lot = tp_distance * contract_size = 0.00054 * 100000 = 54.0
#   prop_lots = round(670 / 54, 2) = 12.41
#   pers_lots = round(12.41 * 0.70, 2) = 8.69
#   pers_dollar_per_lot = sl_distance * contract_size = 0.00200 * 100000 = 200.0
#   pers_dollar_risk = round(8.69 * 200.0, 2) = 1738.0
#   prop_sl = round(tp,5)=1.08554 ; prop_tp = round(sl,5)=1.08300
#   pers_tp = round(tp,5)=1.08554
def test_phase2_geometry_matches_current_formula():
    g = compute_geometry(
        ticker="EURUSD", signal="LONG",
        entry=1.08500, signal_sl=1.08300, signal_tp=1.08554,
        price_digits=5,
        prop_contract_size=100000.0, prop_tick_size=0.00001, prop_tick_value=1.0,
        pers_contract_size=100000.0, pers_tick_size=0.00001, pers_tick_value=1.0,
        baseline_equity=100000.0, prop_risk_pct=0.0067, phase_ratio=0.70,
    )
    assert g["prop_lots"] == 12.41
    assert g["pers_lots"] == 8.69
    assert g["prop_dollar_risk"] == pytest.approx(670.0, abs=0.01)
    assert g["pers_dollar_risk"] == pytest.approx(1738.0, abs=0.01)
    assert g["prop_sl"] == 1.08554          # funded SL = signal TP
    assert g["prop_tp"] == 1.08300          # funded TP = signal SL
    assert g["pers_sl"] == 1.08300          # personal uses webhook sl
    assert g["pers_tp"] == 1.08554          # personal TP = signal TP
    assert g["prop_signal"] == "SHORT"
    assert g["pers_signal"] == "LONG"


def test_phase2_geometry_rejects_zero_tp_distance():
    g = compute_geometry(
        ticker="EURUSD", signal="LONG",
        entry=1.08500, signal_sl=1.08300, signal_tp=1.08500,
        price_digits=5,
        prop_contract_size=100000.0, prop_tick_size=0.00001, prop_tick_value=1.0,
        pers_contract_size=100000.0, pers_tick_size=0.00001, pers_tick_value=1.0,
        baseline_equity=100000.0, prop_risk_pct=0.0067, phase_ratio=0.70,
    )
    assert "reject" in g
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/layer2/test_phase2_strategy.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'layer2.phase2_strategy'`

- [ ] **Step 3: Create `layer2/phase2_strategy.py`**

This is the exact transcription of `logic_core.py:1418–1511` math (USD-quote vs tick formula preserved verbatim; rounding identical):

```python
"""Phase 2 — verbatim extraction of the current Layer 2 geometry (pure).

Math is byte-identical to logic_core.receive_signal lines 1418-1511 as of
commit 56c86bf. Do not "improve" it — the regression test pins exact numbers.
"""
from __future__ import annotations

from layer2.strategy_common import invert_signal


def compute_geometry(
    *,
    ticker: str,
    signal: str,
    entry: float,
    signal_sl: float,
    signal_tp: float,
    price_digits: int,
    prop_contract_size: float,
    prop_tick_size: float,
    prop_tick_value: float,
    pers_contract_size: float,
    pers_tick_size: float,
    pers_tick_value: float,
    baseline_equity: float,
    prop_risk_pct: float,
    phase_ratio: float,
) -> dict:
    """Exact current behaviour. Returns ticket fields or {"reject": reason}."""
    prop_dollar_risk = baseline_equity * prop_risk_pct

    sl_distance = abs(entry - signal_sl)     # personal SL distance (signal perspective)
    tp_distance = abs(signal_tp - entry)     # funded SL distance = signal TP distance

    if tp_distance <= 0:
        return {"reject": f"TP distance is zero (tp={signal_tp} entry={entry})"}

    # Funded SL = signal TP (tight) ; Funded TP = signal SL (wide)
    prop_sl = round(signal_tp, price_digits)
    prop_tp = round(signal_sl, price_digits)

    if ticker.endswith("USD") and prop_contract_size > 0:
        prop_dollar_per_lot = tp_distance * prop_contract_size
    else:
        prop_dollar_per_lot = (tp_distance / prop_tick_size) * prop_tick_value
    prop_lots = round(prop_dollar_risk / prop_dollar_per_lot, 2)

    pers_lots = round(prop_lots * phase_ratio, 2)
    if ticker.endswith("USD") and pers_contract_size > 0:
        pers_dollar_per_lot = sl_distance * pers_contract_size
    else:
        pers_dollar_per_lot = (sl_distance / pers_tick_size) * pers_tick_value
    pers_dollar_risk = round(pers_lots * pers_dollar_per_lot, 2)

    pers_tp = round(signal_tp, price_digits)   # personal TP = signal TP

    return {
        "prop_signal": invert_signal(signal),
        "prop_lots": prop_lots,
        "prop_sl": prop_sl,
        "prop_tp": prop_tp,
        "prop_dollar_risk": round(prop_dollar_risk, 2),
        "pers_signal": signal,
        "pers_lots": pers_lots,
        "pers_sl": round(signal_sl, price_digits),
        "pers_tp": pers_tp,
        "pers_dollar_risk": pers_dollar_risk,
        "sl_distance": sl_distance,
        "tp_distance": tp_distance,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/layer2/test_phase2_strategy.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add layer2/phase2_strategy.py tests/layer2/test_phase2_strategy.py
git commit -m "Extract Phase 2 geometry verbatim into phase2_strategy (pinned by regression test)"
```

---

## Task 6: Wire `receive_signal()` to dispatch geometry by phase

**Files:**
- Modify: `layer2/logic_core.py` (imports near line 67; body lines 1418–1572)

The Phase 2 branch must produce **identical** tickets to today. Phase 1 uses the new module and signal SL as the anchor (it ignores the Layer 0 TP).

- [ ] **Step 1: Add strategy imports**

In `layer2/logic_core.py`, after the existing `from layer2 import telegram_handlers` (line 67), add:

```python
from layer2 import phase1_strategy, phase2_strategy
from layer2.state import (
    _phase1_load, _phase1_active_stage, _phase1_advance_stage,
)
```

(`_phase1_*` helpers are created in Task 8 — this import line is added together with that task; if implementing Task 6 first, add only the `phase1_strategy, phase2_strategy` import and add the `_phase1_*` import in Task 8.)

- [ ] **Step 2: Replace the geometry block (logic_core.py lines 1442–1511)**

Find this exact current block (lines 1442–1511, beginning `# Funded account SL/TP are the exact swap` … ending `pers_tp = round(payload.tp, price_digits)   # personal TP = signal TP`) and replace the whole region from line 1442 through line 1486 (the `logger.info("LOTS …")` call inclusive) with:

```python
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
```

(Note: the earlier block at lines 1409–1441 — `prop_dollar_risk = baseline_equity * PROP_RISK_PCT`, `phase_ratio = …`, `sl_distance`/`tp_distance`, the `prop_tick_size`/`prop_tick_val` reads and their `<=0` guard, and the `tp_distance <= 0` guard — stays. The Phase 2 module recomputes risk/distances internally but the kept guards still protect the worker contract data. The lines being replaced are only 1442–1486.)

- [ ] **Step 3: Update ticket construction (logic_core.py lines 1488–1511)**

Replace the `prop_ticket` / `pers_ticket` dict construction so it uses the dispatched values instead of the old inline ones:

```python
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
```

- [ ] **Step 4: Update the `_verify_and_notify` call (logic_core.py lines 1535–1555)**

Change `pers_sl=payload.sl,` to `pers_sl=pers_sl,` in the `asyncio.create_task(_verify_and_notify(...))` argument list. All other args already reference the variables now set from `g`.

- [ ] **Step 5: Run the Phase 2 regression + Phase 1 unit suites**

Run: `uv run --extra dev pytest tests/ -q`
Expected: PASS (all tests). The Phase 2 module is unchanged → its regression test still pins identical numbers, proving `receive_signal`'s Phase 2 path is byte-identical.

- [ ] **Step 6: Syntax/boot check**

Run: `uv run python -c "import ast; ast.parse(open('layer2/logic_core.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add layer2/logic_core.py
git commit -m "Dispatch trade geometry by phase in receive_signal"
```

---

## Task 7: Buffer change −1.0pp → −0.5pp (both phases)

**Files:**
- Modify: `layer2/state.py:259` and `layer2/state.py:386-391`
- Test: `tests/layer2/test_buffers.py`

- [ ] **Step 1: Write the failing test**

`tests/layer2/test_buffers.py`:

```python
from layer2.state import _apply_buffers, _p2_display


def test_daily_dd_buffer_is_half_point():
    raw = {
        "max_drawdown_daily_pct": 3.0,
        "max_drawdown_overall_pct": 6.0,
        "profit_target_pct": 10.0,
        "consistency_threshold_pct": 30.0,
    }
    eff = _apply_buffers(raw)
    assert eff["max_drawdown_daily_pct"] == 2.5          # 3.0 - 0.5
    assert eff["max_drawdown_overall_pct"] == 6.0        # unbuffered
    assert eff["daily_profit_cap_pct"] == 2.5            # 25% of 10
    assert eff["consistency_threshold_pct"] == 29.0      # still -1.0pp


def test_p2_display_daily_shows_half_point():
    assert _p2_display("max_drawdown_daily_pct", 3.0) == \
        "3.0% (enforced at 2.5% after −0.5pp buffer)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/layer2/test_buffers.py -q`
Expected: FAIL — daily buffer is 2.0 (still −1.0pp) and display says "−1pp".

- [ ] **Step 3: Edit `layer2/state.py` line 259**

Change:

```python
    effective["max_drawdown_daily_pct"]      = round(raw["max_drawdown_daily_pct"]          - 1.0, 2)
```

to:

```python
    effective["max_drawdown_daily_pct"]      = round(raw["max_drawdown_daily_pct"]          - 0.5, 2)
```

Also update the docstring line 253 from `subtract 1 percentage point` to `subtract 0.5 percentage point`.

- [ ] **Step 4: Edit `layer2/state.py` lines 386–387 (`_p2_display`)**

Change:

```python
    if key == "max_drawdown_daily_pct":
        return f"{value:.1f}% (enforced at {value - 1.0:.1f}% after −1pp buffer)"
```

to:

```python
    if key == "max_drawdown_daily_pct":
        return f"{value:.1f}% (enforced at {value - 0.5:.1f}% after −0.5pp buffer)"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/layer2/test_buffers.py -q`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add layer2/state.py tests/layer2/test_buffers.py
git commit -m "Loosen daily-DD buffer from -1.0pp to -0.5pp (both phases)"
```

---

## Task 8: Phase 1 state block + ratchet persistence helpers

**Files:**
- Modify: `layer2/state.py` (append helpers after `_save_phase`, ~line 112)
- Modify: `config/phase_config.json` (no manual edit needed — created lazily)
- Test: `tests/layer2/test_phase1_state.py`

- [ ] **Step 1: Write the failing test**

`tests/layer2/test_phase1_state.py`:

```python
import json

from layer2 import state


def test_phase1_init_and_load(tmp_path, monkeypatch):
    pc = tmp_path / "phase_config.json"
    pc.write_text(json.dumps({"phase": 1, "active": True}))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)

    state._phase1_init(first_reward=9000.0, fixed_risk=2000.0,
                        stages=[109000.0, 109500.0, 110000.0])
    p1 = state._phase1_load()
    assert p1["first_reward"] == 9000.0
    assert p1["fixed_risk"] == 2000.0
    assert p1["stages"] == [109000.0, 109500.0, 110000.0]
    assert p1["active_stage_index"] == 0
    # persisted to disk
    on_disk = json.loads(pc.read_text())
    assert on_disk["phase1"]["fixed_risk"] == 2000.0


def test_phase1_active_stage_ratchets_and_persists(tmp_path, monkeypatch):
    pc = tmp_path / "phase_config.json"
    pc.write_text(json.dumps({"phase": 1, "active": True}))
    monkeypatch.setattr(state, "PHASE_CONFIG_PATH", pc)
    state._phase1_init(9000.0, 2000.0, [109000.0, 109500.0, 110000.0])

    assert state._phase1_active_stage([109000.0, 109500.0, 110000.0], 100000.0) == 0
    # reaching S1 advances the persisted pointer
    assert state._phase1_active_stage([109000.0, 109500.0, 110000.0], 109000.0) == 1
    assert json.loads(pc.read_text())["phase1"]["active_stage_index"] == 1
    # a loss never reverts it
    assert state._phase1_active_stage([109000.0, 109500.0, 110000.0], 107000.0) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_state.py -q`
Expected: FAIL — `AttributeError: module 'layer2.state' has no attribute '_phase1_init'`

- [ ] **Step 3: Append helpers to `layer2/state.py` after `_save_phase` (after line 111)**

```python
# ── Phase 1 strategy state (nested 'phase1' block in phase_config.json) ────

_phase1_lock = threading.Lock()


def _phase1_load() -> dict:
    with _phase1_lock:
        data = _load_phase()
        return dict(data.get("phase1", {}))


def _phase1_init(first_reward: float, fixed_risk: float, stages: list[float]) -> None:
    """Write a fresh phase1 block (called at /phase1 confirm). Resets the ratchet."""
    with _phase1_lock:
        data = _load_phase()
        data["phase1"] = {
            "first_reward": round(first_reward, 2),
            "fixed_risk": round(fixed_risk, 2),
            "stages": [round(s, 2) for s in stages],
            "active_stage_index": 0,
            "max_prop_lots": float(data.get("phase1", {}).get("max_prop_lots", 0.0)),
            "profitable_days": 0,
            "last_stage_day": "never",
        }
        _save_phase(data)


def _phase1_active_stage(stages: list[float], current_equity: float) -> int:
    """Ratcheting active-stage index. Persists advances. Never reverts."""
    from layer2.phase1_strategy import active_stage_index
    with _phase1_lock:
        data = _load_phase()
        p1 = data.get("phase1", {})
        prev = int(p1.get("active_stage_index", 0))
        idx = active_stage_index(stages, current_equity, prev)
        if idx != prev:
            p1["active_stage_index"] = idx
            data["phase1"] = p1
            _save_phase(data)
        return idx


def _phase1_record_stage_day(day_str: str) -> None:
    """Increment the profitable-day counter and stamp the day a stage was hit."""
    with _phase1_lock:
        data = _load_phase()
        p1 = data.setdefault("phase1", {})
        if p1.get("last_stage_day") != day_str:
            p1["profitable_days"] = int(p1.get("profitable_days", 0)) + 1
            p1["last_stage_day"] = day_str
            data["phase1"] = p1
            _save_phase(data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_state.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Add the `_phase1_*` import to `logic_core.py`**

Add to the import inserted in Task 6 Step 1 (if not already present):

```python
from layer2.state import (
    _phase1_load, _phase1_active_stage, _phase1_record_stage_day,
)
```

Run: `uv run python -c "import ast; ast.parse(open('layer2/logic_core.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add layer2/state.py tests/layer2/test_phase1_state.py layer2/logic_core.py
git commit -m "Add persisted Phase 1 state block with ratcheting active-stage helpers"
```

---

## Task 9: Phase 1 kill set + stage-win day-halt in `_run_equity_check()`

**Files:**
- Modify: `layer2/logic_core.py` lines 854–997 (kill block)
- Test: `tests/layer2/test_phase1_kills.py`

Phase 1 kill set: K1 ON, K3 OFF, K2 ON, K4 ON, K5 OFF, plus a **stage-win day-halt** (reach active stage → close + daily halt + ratchet + auto-resume next session). Phase 2 kills are untouched.

The pure decision logic goes in `phase1_strategy.evaluate_kills()`; the orchestrator performs the side effects (force-close, state writes, alerts) exactly as it does today.

- [ ] **Step 1: Write the failing test**

`tests/layer2/test_phase1_kills.py`:

```python
import pytest
from layer2.phase1_strategy import evaluate_kills


_CFG = dict(baseline=100000.0, day_start=100000.0,
            dd_daily_pct=2.5, dd_overall_pct=6.0)


def test_k2_overall_floor():
    r = evaluate_kills(prop_equity=93999.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r["reason"] == "overall_drawdown_limit"
    assert r["permanent"] is True


def test_k4_final_stage_profit_target():
    r = evaluate_kills(prop_equity=110000.0, stages=[109000, 109500, 110000],
                       active_index=2, **_CFG)
    assert r["reason"] == "profit_target"
    assert r["permanent"] is True


def test_k1_daily_loss():
    # day_start 100000, 2.5% -> floor 97500
    r = evaluate_kills(prop_equity=97400.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r["reason"] == "daily_loss_limit"
    assert r["permanent"] is False


def test_stage_win_day_halt():
    r = evaluate_kills(prop_equity=109000.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r["reason"] == "phase1_stage_reached"
    assert r["permanent"] is False
    assert r["stage_value"] == 109000


def test_no_k3_daily_profit_cap():
    # well above day_start but below the stage -> nothing fires (no K3 in phase 1)
    r = evaluate_kills(prop_equity=108000.0, stages=[109000, 109500, 110000],
                       active_index=0, **_CFG)
    assert r is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_kills.py -q`
Expected: FAIL — `ImportError: cannot import name 'evaluate_kills'`

- [ ] **Step 3: Append `evaluate_kills` to `layer2/phase1_strategy.py`**

```python
def evaluate_kills(
    *,
    prop_equity: float,
    baseline: float,
    day_start: float,
    dd_daily_pct: float,
    dd_overall_pct: float,
    stages: list[float],
    active_index: int,
) -> dict | None:
    """Phase 1 kill decision (pure). Priority: K2 > K1 > stage-win > K4.

    Returns None or {reason, permanent, stage_value?}.
      - overall_drawdown_limit (K2)  permanent
      - daily_loss_limit       (K1)  not permanent (auto-resume next session)
      - phase1_stage_reached         not permanent (day halt; ratchet advances)
      - profit_target          (K4)  permanent (final stage reached)
    No K3 (daily profit cap) and no K5 (consistency) in Phase 1.
    """
    # K2 — static overall floor (permanent)
    if dd_overall_pct > 0 and baseline > 0:
        overall_floor = baseline - round(baseline * dd_overall_pct / 100.0, 2)
        if prop_equity <= overall_floor:
            return {"reason": "overall_drawdown_limit", "permanent": True,
                    "overall_floor": overall_floor}

    # K1 — dynamic daily floor (not permanent)
    if dd_daily_pct > 0 and day_start > 0:
        daily_floor = day_start - round(day_start * dd_daily_pct / 100.0, 2)
        if prop_equity <= daily_floor:
            return {"reason": "daily_loss_limit", "permanent": False,
                    "daily_floor": daily_floor}

    if not stages:
        return None

    # K4 — final stage (funded line) reached -> permanent
    if prop_equity >= stages[-1]:
        return {"reason": "profit_target", "permanent": True,
                "stage_value": stages[-1]}

    # Stage-win day-halt — reached the active stage (not the final one)
    if 0 <= active_index < len(stages) and prop_equity >= stages[active_index]:
        return {"reason": "phase1_stage_reached", "permanent": False,
                "stage_value": stages[active_index]}

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --extra dev pytest tests/layer2/test_phase1_kills.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Branch the kill block in `_run_equity_check()`**

In `layer2/logic_core.py`, the kill block runs lines 865–997. Wrap the **existing** K1–K5 code (lines 865–997) in `if phase != 1:` (indent the existing block one level) and add the Phase 1 branch before it:

```python
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
                f"System auto-resumes next session; next target is the following stage."
            )
        elif reason == "daily_loss_limit":
            df = decision["daily_floor"]
            _alert_sync(
                f"🔴 <b>KILL 1 — Daily Loss Limit Hit (Phase 1)</b>\n\n"
                f"Equity: <b>${prop_equity:,.2f}</b>  |  Daily floor: ${df:,.2f}\n"
                f"Day-start: ${day_start:,.2f}\n\n"
                f"All positions force-closed. Auto-resumes next session."
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
        # ── Existing Phase 2 kill block (K1–K5) — UNCHANGED ──
        # (the original lines 865-997 live here, indented one level)
        ...
```

(Mechanically: select original lines 865–997, indent them by 4 spaces under `if phase != 1:`, and paste the Phase 1 branch immediately above. Do not edit any logic inside the Phase 2 block.)

- [ ] **Step 6: Full suite + syntax check**

Run: `uv run --extra dev pytest tests/ -q && uv run python -c "import ast; ast.parse(open('layer2/logic_core.py').read()); print('ok')"`
Expected: all tests PASS, then `ok`.

- [ ] **Step 7: Commit**

```bash
git add layer2/logic_core.py layer2/phase1_strategy.py tests/layer2/test_phase1_kills.py
git commit -m "Add Phase 1 kill set and stage-win day-halt; isolate Phase 2 kills"
```

---

## Task 10: Disable news gating in Phase 1

**Files:**
- Modify: `layer2/logic_core.py` — `_run_news_preclose_check()` (line 480) and the news gate in `receive_signal()` (lines 1317–1333)

- [ ] **Step 1: Guard the news pre-close loop**

In `layer2/logic_core.py`, `_run_news_preclose_check()` begins:

```python
def _run_news_preclose_check() -> None:
    global _news_closed_events

    now            = datetime.now(timezone.utc)
```

Insert a phase guard right after `global _news_closed_events`:

```python
def _run_news_preclose_check() -> None:
    global _news_closed_events

    with _state_lock:
        if int(_phase_state.get("phase", 1)) == 1:
            return  # Phase 1 (evaluation): no prop-firm news rule — skip pre-close

    now            = datetime.now(timezone.utc)
```

- [ ] **Step 2: Skip the news suppression gate in `receive_signal()` for Phase 1**

In `receive_signal()` the gate is (lines 1318–1323):

```python
    now_utc = datetime.now(timezone.utc)
    with _news_suppressed_lock:
        news_block = payload.ticker in _news_suppressed_pairs and _news_suppressed_pairs[payload.ticker] > now_utc
    with _manual_suppress_lock:
        manual_block = payload.ticker in _manual_suppressed_pairs
```

Change the `news_block` line so it is forced off in Phase 1 (manual `/closepair` block is an explicit operator action — keep it):

```python
    now_utc = datetime.now(timezone.utc)
    with _news_suppressed_lock:
        news_block = (
            phase != 1
            and payload.ticker in _news_suppressed_pairs
            and _news_suppressed_pairs[payload.ticker] > now_utc
        )
    with _manual_suppress_lock:
        manual_block = payload.ticker in _manual_suppressed_pairs
```

(`phase` is already in scope — read at lines 1287–1291.)

- [ ] **Step 3: Syntax check**

Run: `uv run python -c "import ast; ast.parse(open('layer2/logic_core.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add layer2/logic_core.py
git commit -m "Disable news pre-close and news suppression in Phase 1"
```

---

## Task 11: `/phase1` ConversationHandler wizard

**Files:**
- Modify: `layer2/telegram_handlers.py` — add state consts (~line 65), import, wizard handlers, refactor `_cmd_phase1`, register wizard, remove old plain handler (line 2206)

- [ ] **Step 1: Add wizard state constants**

In `layer2/telegram_handlers.py`, after line 65 (`UPDATE_LAYER3_CHOOSE = 19`) add:

```python
P1_INPUT   = 20
P1_CONFIRM = 21
```

- [ ] **Step 2: Add imports**

Extend the `from layer2.state import (...)` block (lines 17–35) to also import:

```python
    _phase1_init, _phase1_load,
```

And add a new import line below it:

```python
from layer2 import phase1_strategy
```

- [ ] **Step 3: Replace `_cmd_phase1` (lines 575–639) with wizard entry + steps**

Replace the entire current `_cmd_phase1` function (lines 575–639) with:

```python
async def _cmd_phase1(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _auth(update):
        return ConversationHandler.END
    _wizard_data.clear()
    await update.message.reply_text(
        "⚙️ <b>Phase 1 Setup</b>\n\n"
        "Send first-trade  <code>reward:risk</code>  (in $)\n"
        "   e.g.  <code>9000:2000</code>\n\n"
        "• <b>Reward</b> — profit target of your FIRST Phase 1 "
        "trade (sets Stage 1 = baseline + this).\n"
        "• <b>Risk</b> — fixed $ lost if any trade hits SL. "
        "Identical for every trade.\n\n"
        "ℹ️ Remaining stages are spread automatically:\n"
        "   (overall target − first reward) ÷ (min profitable days − 1)\n\n"
        "/cancel to abort.",
        parse_mode="HTML",
    )
    return P1_INPUT


async def _p1_input(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        first_reward, fixed_risk = phase1_strategy.parse_reward_risk(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(
            f"⚠️ <b>Invalid Input</b>\n\n{exc}\n\n"
            f"Format: <code>reward:risk</code> e.g. <code>9000:2000</code>",
            parse_mode="HTML",
        )
        return P1_INPUT

    with _pf_lock:
        pf = dict(_propfirm)
    baseline       = pf.get("baseline_equity", 0.0)
    target_pct     = pf.get("profit_target_pct", 0.0)
    min_days       = int(pf.get("min_profit_days", 0))
    overall_dd_pct = pf.get("max_drawdown_overall_pct", 0.0)

    if baseline <= 0:
        balance, err = await asyncio.to_thread(_lock_baseline_from_live)
        if err:
            await update.message.reply_text(
                f"⚠️ <b>Baseline Missing</b>\n\nCould not set baseline: <code>{err}</code>\n\n"
                f"Run /changepropfirm first, then /phase1 again.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        baseline = balance

    verr = phase1_strategy.validate_phase1_inputs(
        first_reward, fixed_risk, baseline, target_pct, min_days)
    if verr:
        await update.message.reply_text(
            f"⚠️ <b>Cannot Configure Phase 1</b>\n\n{verr}",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    stages = phase1_strategy.derive_stages(baseline, first_reward, target_pct, min_days)
    target_amt  = baseline * target_pct / 100.0
    overall_amt = baseline * overall_dd_pct / 100.0
    _wizard_data["p1"] = {
        "first_reward": first_reward, "fixed_risk": fixed_risk,
        "stages": stages, "baseline": baseline,
    }
    stage_str = "  →  ".join(f"${s:,.0f}" for s in stages)
    warn = ""
    if fixed_risk >= overall_amt - (baseline - stages[0]):
        warn = ""  # placeholder; no daily-DD figure available pre-session
    daily_room = baseline * pf.get("max_drawdown_daily_pct", 0.0) / 100.0
    if daily_room > 0 and fixed_risk >= daily_room:
        warn = (f"\n\n⚠️ Risk ${fixed_risk:,.0f} ≥ daily-DD room ${daily_room:,.0f} "
                f"— only one losing trade fits per day.")

    await update.message.reply_text(
        f"✅ <b>Phase 1 Ready</b>\n\n"
        f"First reward : ${first_reward:,.0f}  → Stage 1 = ${stages[0]:,.0f}\n"
        f"Fixed risk   : ${fixed_risk:,.0f}   (every trade)\n"
        f"Stages       : {stage_str}\n"
        f"Overall stop / target : ${baseline - overall_amt:,.0f} / ${baseline + target_amt:,.0f}"
        f"{warn}\n\n"
        f"Reply <code>CONFIRM</code> to proceed.\n"
        f"Send /cancel to abort.",
        parse_mode="HTML",
    )
    return P1_CONFIRM


async def _p1_confirm(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if (update.message.text or "").strip() != "CONFIRM":
        await update.message.reply_text(
            "⚠️ <b>Confirmation Required</b>\n\nType <code>CONFIRM</code> to proceed, or /cancel to abort.",
            parse_mode="HTML",
        )
        return P1_CONFIRM

    d = _wizard_data.get("p1")
    if not d:
        await update.message.reply_text("⚠️ Session expired. Run /phase1 again.", parse_mode="HTML")
        return ConversationHandler.END

    with _state_lock:
        _phase_state["phase"] = 1
        _phase_state.pop("permanently_halted", None)
        _phase_state.pop("phase1_permanently_halted", None)
        _save_phase(_phase_state)

    _phase1_init(d["first_reward"], d["fixed_risk"], d["stages"])
    await asyncio.to_thread(_dispatch_parameters)
    _wizard_data.clear()

    stage_str = "  →  ".join(f"${s:,.0f}" for s in d["stages"])
    await update.message.reply_text(
        f"🟢 <b>Phase 1 Active</b>\n\n"
        f"Personal multiplier: ×{PHASE_MULT[1]:.2f}\n"
        f"Prop baseline: ${d['baseline']:,.2f}\n"
        f"Fixed risk: ${d['fixed_risk']:,.0f} / trade\n"
        f"Stages: {stage_str}\n\n"
        f"<b>Next Step</b>\n/resume",
        parse_mode="HTML",
    )
    logger.info("Telegram: phase 1 configured  reward=%.2f risk=%.2f stages=%s",
                d["first_reward"], d["fixed_risk"], d["stages"])
    return ConversationHandler.END


async def _p1_cancel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> int:
    _wizard_data.clear()
    await update.message.reply_text("❌ Phase 1 setup cancelled.", parse_mode="HTML")
    return ConversationHandler.END
```

- [ ] **Step 4: Register the wizard and remove the old plain handler**

In `_run_bot()` (line 2200+), add a ConversationHandler next to the others and register it; **delete** the existing plain registration at line 2206 `tg_app.add_handler(CommandHandler("phase1", _cmd_phase1))`.

Add after `setwindow_wizard = ConversationHandler(...)` block (after line 2198):

```python
    phase1_wizard = ConversationHandler(
        entry_points=[CommandHandler("phase1", _cmd_phase1)],
        states={
            P1_INPUT:   [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p1_input)],
            P1_CONFIRM: [MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, _p1_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _p1_cancel)],
        per_chat=True,
    )
```

Add `tg_app.add_handler(phase1_wizard)` next to the other `add_handler(...wizard)` calls (after line 2205, `tg_app.add_handler(setwindow_wizard)`).

Delete line 2206: `tg_app.add_handler(CommandHandler("phase1",        _cmd_phase1))`.

- [ ] **Step 5: Syntax check + import check**

Run:
```bash
uv run python -c "import ast; ast.parse(open('layer2/telegram_handlers.py').read()); print('ok')"
TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=1 uv run python -c "import layer2.telegram_handlers; print('import ok')"
```
Expected: `ok` then `import ok`.

- [ ] **Step 6: Commit**

```bash
git add layer2/telegram_handlers.py
git commit -m "Convert /phase1 into a reward:risk setup wizard"
```

---

## Task 12: Phase 1 "Trade Opened" alert + final verification

**Files:**
- Modify: `layer2/logic_core.py` — `_verify_and_notify_inner` (lines 1124–1230)
- Modify: `CLAUDE.md` (Build Status / Current State), `docs/superpowers/specs/...` (none)

- [ ] **Step 1: Make the open-trade alert phase-aware**

In `_verify_and_notify_inner`, the reward/RR are recomputed from distances at lines 1124–1128:

```python
    pers_reward = round(pers_dollar_risk * (tp_distance / sl_distance), 2) if sl_distance > 0 else 0.0
    prop_reward = round(prop_dollar_risk * (sl_distance / tp_distance), 2) if tp_distance > 0 else 0.0
    pers_rr = tp_distance / sl_distance if sl_distance > 0 else 0.0
    prop_rr = sl_distance / tp_distance if tp_distance > 0 else 0.0
```

These formulas are Phase-2 geometry assumptions. For Phase 1 they are wrong (TP is overridden). Pass the phase through and, for Phase 1, read the figures from the geometry result instead. Add two optional kwargs to both `_verify_and_notify` and `_verify_and_notify_inner`: `prop_reward_in: float = 0.0, pers_reward_in: float = 0.0, prop_rr_in: float = 0.0, pers_rr_in: float = 0.0`. Then replace the four lines above with:

```python
    if phase == 1:
        pers_reward, prop_reward = pers_reward_in, prop_reward_in
        pers_rr, prop_rr = pers_rr_in, prop_rr_in
    else:
        pers_reward = round(pers_dollar_risk * (tp_distance / sl_distance), 2) if sl_distance > 0 else 0.0
        prop_reward = round(prop_dollar_risk * (sl_distance / tp_distance), 2) if tp_distance > 0 else 0.0
        pers_rr = tp_distance / sl_distance if sl_distance > 0 else 0.0
        prop_rr = sl_distance / tp_distance if tp_distance > 0 else 0.0
```

In `receive_signal`'s `asyncio.create_task(_verify_and_notify(...))` call, pass:

```python
        prop_reward_in=g.get("prop_reward", 0.0),
        pers_reward_in=g.get("pers_reward", 0.0),
        prop_rr_in=g.get("prop_rr", 0.0),
        pers_rr_in=g.get("pers_rr", 0.0),
```

and forward the same kwargs from `_verify_and_notify` into `_verify_and_notify_inner` (mirror the existing pass-through pattern at lines 1081–1088).

- [ ] **Step 2: Add a Phase 1 context line to the "Trade Opened" message**

In the `prop_filled and pers_filled` branch (the `await _telegram_alert(f"<b>{ticker} — Trade Opened</b> …")` at lines 1207–1230), change the trailing `<b>Context</b>` block to:

```python
            f"<b>Context</b>\n"
            f"Phase: Phase {phase}"
            + (f"\nActive stage: ${_phase1_load().get('stages', ['?'])[min(int(_phase1_load().get('active_stage_index',0)), len(_phase1_load().get('stages',[1]))-1)]:,.0f}"
               if phase == 1 else f"\nBaseline: ${baseline_equity:,.2f}")
```

(Phase 2 string is unchanged; Phase 1 shows the active stage instead of baseline.)

- [ ] **Step 3: Full suite + syntax check**

Run: `uv run --extra dev pytest tests/ -q && uv run python -c "import ast; ast.parse(open('layer2/logic_core.py').read()); print('ok')"`
Expected: all PASS, `ok`.

- [ ] **Step 4: Update `CLAUDE.md` status**

In `CLAUDE.md`, update the Layer 2 Build Status row and "Current State" to note: Phase 1/Phase 2 strategy split shipped; Phase 1 is dynamic reward-targeting; pending `/update layer2`. Remove the now-resolved pending close-alert reminder only if Warren confirms (leave it otherwise).

- [ ] **Step 5: Commit**

```bash
git add layer2/logic_core.py CLAUDE.md
git commit -m "Phase-aware Trade Opened alert (Phase 1 stage context)"
```

---

## Task 13: Final regression gate + deployment note

- [ ] **Step 1: Full test run**

Run: `uv run --extra dev pytest tests/ -v`
Expected: every test passes. The Phase 2 regression test (`test_phase2_strategy.py`) passing is the proof that live funded behaviour is byte-identical except the approved −0.5pp daily buffer.

- [ ] **Step 2: Import smoke test**

Run:
```bash
TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=1 uv run python -c "import layer2.logic_core; print('logic_core import ok')"
```
Expected: `logic_core import ok` (module imports; threads start as daemons — Ctrl-C / process exit is fine).

- [ ] **Step 3: Push and report deployment**

```bash
git push origin main
```

Then tell Warren: deploy with **`/update layer2`** on VPS #1 (Layer 2-only; `pyproject.toml` changed only by the pytest config block, which does not affect runtime — `uv sync` not required). Walk Cases 1–3 on demo before any live capital (spec §14).

---

## Self-Review

**1. Spec coverage:**

| Spec section | Task(s) |
|---|---|
| §2 file split / pure modules | 2, 5, 6, 9 |
| §3 locked geometry (anchor, mirror, lots×0.2) | 4 |
| §4 active-stage ratchet + persistence | 3, 8 |
| §5 stage derivation + `/phase1` wizard wording (CONFIRM/cancel, cuts) | 3, 11 |
| §6 day model — halt on stage-win, auto-resume | 9 |
| §7 kill set K1 on / K3,K5 off / K2 / K4; news off | 9, 10 |
| §8 −0.5pp daily buffer both phases | 7 |
| §9 per-signal flow, live equity at signal | 6 |
| §10 Phase 1 Trade Opened alert | 12 |
| §11 phase1 state block schema | 8 |
| §12 edge cases (reject zero-D, lots→0, reward≤0, max lots, W1≥target, min_days<2) | 4, 3 |
| §13 Phase 2 no-op regression | 5, 6, 13 |
| §14 testing (Cases 1–3 numbers) | 4, 9 |
| §15 deployment `/update layer2` | 13 |

No spec requirement is unmapped.

**2. Placeholder scan:** One intentional empty `warn = ""` placeholder in Task 11 Step 3 is immediately overwritten by the daily-room check below it; left for clarity of the two-branch logic. No "TBD/TODO" code. All code steps contain full code.

**3. Type consistency:** `compute_geometry` returns the same key set used by Task 6's unpacking (`prop_signal/prop_lots/prop_sl/prop_tp/prop_dollar_risk/pers_*/sl_distance/tp_distance`). `phase1_strategy.active_stage_index(stages, equity, prev)` (3 args) matches `state._phase1_active_stage` caller. `evaluate_kills` keys (`reason/permanent/stage_value/daily_floor/overall_floor`) match Task 9 Step 5 usage. `_phase1_load/_phase1_init/_phase1_active_stage/_phase1_record_stage_day` names consistent across Tasks 6, 8, 9, 11.

**Implementation-plan decisions (settled):** phase1 state lives in a nested `phase1` block in `phase_config.json`; news disabled via Layer-2 skip (Layer 1 untouched); lot-explosion guard is the configurable `phase1.max_prop_lots` (0 = disabled) since the worker equity payload exposes no `volume_max` — adding `volume_max` to Layer 3 `_query_equity` is noted future work, out of scope per spec §16.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-16-phase1-strategy.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task with two-stage review between tasks; fast iteration and the Phase 2 regression gate is checked after every code task.

**2. Inline Execution** — execute tasks in this session via executing-plans, batched with review checkpoints.

Which approach?
