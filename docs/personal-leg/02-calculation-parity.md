# Calculation Parity — "run both legs together, then reverse it onto personal"

This is the proof behind Warren's instruction: *"how the prop is calculated, use that logic to
calculate, just in a reverse manner … run yourself as if when two run together, then apply it at the
personal leg."* Everything here is taken **verbatim from the live code** so the standalone leg
reproduces the kernel exactly — only the prop dependency is removed.

Sources: `layer2/phase2_strategy.py` (`compute_geometry`), `layer2/strategy_common.py`
(`dollar_per_unit`), `layer2/phase1_strategy.py:140-178`.

---

## Step 0 — the shared kernel (UNCHANGED, copy as-is)

`layer2/strategy_common.py:13` — dollars per lot at price-distance `X` is `X × k`:
```python
def dollar_per_unit(ticker, contract_size, tick_size, tick_value):
    if ticker.endswith("USD") and contract_size > 0:
        return contract_size            # xxxUSD: P&L already USD/unit
    return tick_value / tick_size       # else: broker tick math
```
Both legs use this. The standalone personal leg keeps it byte-for-byte.

---

## Step 1 — run the CURRENT 2-leg system (Phase 2), code-accurate

From `phase2_strategy.compute_geometry`. A **LONG** signal (signal direction = the personal side):

```
INPUTS
  signal=LONG, entry=1.08500, signal_sl=1.08300, signal_tp=1.08554, price_digits=5
  contract_size=100000, tick_size=0.00001, tick_value=1.0   (EURUSD → k = 100000)
  baseline_equity=100000, prop_risk_pct=0.01, phase_ratio=0.70   (Phase 2)

DISTANCES
  sl_distance = |entry - signal_sl| = |1.08500 - 1.08300| = 0.00200   (FAR / wide)
  tp_distance = |signal_tp - entry| = |1.08554 - 1.08500| = 0.00054   (NEAR / tight)

PROP LEG  (the authoritative math; prop = inverse = SHORT)
  prop_dollar_risk   = baseline * prop_risk_pct      = 100000 * 0.01     = 1000.0
  prop_dollar_per_lot= tp_distance * k               = 0.00054 * 100000  = 54.0   # sized over PROP stop = NEAR
  prop_lots          = round(1000.0 / 54.0, 2)                            = 18.52
  prop_sl            = signal_tp = 1.08554           # prop stop (near)
  prop_tp            = signal_sl = 1.08300           # prop target (far) → prop wins big

PERSONAL LEG  (DERIVED from prop today — this is the parasitic part)
  pers_lots          = round(prop_lots * phase_ratio, 2) = round(18.52 * 0.70, 2) = 12.96
  pers_sl            = signal_sl = 1.08300           # FAR
  pers_tp            = signal_tp = 1.08554           # NEAR
  pers_dollar_per_lot= sl_distance * k = 0.00200 * 100000 = 200.0
  pers_dollar_risk   = round(12.96 * 200.0, 2)                            = 2592.0
```

**Reading it:** the prop is the leg with the self-contained method — fixed `risk_$ = baseline × pct`
sized over **its** stop (the near distance). The personal leg only exists as `prop_lots × 0.70`, then
stopped at the far level → it actually risks **$2,592** (≈ 2.59% of baseline), a number that **floats**
with each signal's near/far ratio. Personal has no anchor of its own.

> Phase 1 is worse (`phase1_strategy.py:171`): `pers_sl = prop_tp`, a level computed from the prop
> stage-ladder gap and live prop equity. Remove the prop and the Phase-1 personal leg has **no SL at
> all**. That's why Phase 1's reward-targeting scheme **cannot** be reused — it is intrinsically
> prop-coupled. The standalone uses the Phase-2-style box for everything.

---

## Step 2 — reverse it onto the personal leg (the standalone)

The prop's method = *"risk a fixed `baseline × pct` over this leg's own stop; box from the signal."*
Apply the **same method** to personal, in the **reverse direction**:

| Prop (inverse leg) | Personal (signal leg) — the reverse |
|---|---|
| direction = invert(signal) | direction = **signal** |
| stop = `signal_tp` → sizes over **near** `tp_distance` | stop = `signal_sl` → sizes over **far** `sl_distance` |
| `risk_$ = baseline × prop_risk_pct` | `risk_$ = personal_baseline × risk_pct` |
| `lots = risk_$ / (tp_distance × k)` | `lots = risk_$ / (sl_distance × k)` |

Same formula, opposite end of the same SL/TP box. Native function:

```
risk_$         = personal_baseline * risk_pct          # NATIVE anchor (active mode's pct)
sl_distance    = abs(entry - signal_sl)                # personal's OWN stop (far)
dollar_per_lot = sl_distance * k                       # k from dollar_per_unit (unchanged)
lots           = round(risk_$ / dollar_per_lot, 2)
direction      = signal
sl             = round(signal_sl, price_digits)
tp             = round(signal_tp, price_digits)
```

### Worked numbers — same signal, personal_baseline=100000 SGD, risk_pct=1%
```
risk_$         = 100000 * 0.01 = 1000.0
dollar_per_lot = 0.00200 * 100000 = 200.0
lots           = round(1000.0 / 200.0, 2) = 5.00
direction      = LONG ; sl = 1.08300 ; tp = 1.08554
risk taken     = 5.00 * 0.00200 * 100000 = 1000.0    # EXACTLY baseline × pct, every trade
target gain    = 5.00 * 0.00054 * 100000 = 270.0     # realized RR = 270/1000 = 0.27 (== signal RR)
```

**Parity result:** identical kernel, identical direction, identical geometry, identical RR. The only
deltas are intentional and desired:
- **Anchor:** prop's `baseline` → personal's own `personal_baseline` (no prop needed).
- **Risk is now constant** (`$1,000` = 1% every trade) instead of floating (`$2,592` ≈ 2.59%).

> To make the standalone risk-match today's *effective* personal exposure instead of running a clean 1%,
> set `risk_pct ≈ 2.6%` (≈ `prop_risk_pct × phase_ratio × sl_distance/tp_distance` at the current signal
> shape). Recommended instead: pick `risk_pct` deliberately — the whole point of the rebuild is a true,
> constant per-trade risk. **Confirm with Warren.**

---

## Step 3 — first regression test (the builder writes this first)

Mirror `tests/layer2/test_phase2_strategy.py`. The standalone `compute_personal_geometry` for the inputs
above must return: `lots == 5.00`, `sl == 1.08300`, `tp == 1.08554`, `direction == "LONG"`,
`dollar_risk == 1000.0`. A zero `sl_distance` must return `{"reject": ...}`.

For a **SHORT** signal the geometry is symmetric: `direction=SHORT`, `sl=signal_sl` (above entry),
`tp=signal_tp` (below entry), `sl_distance=|entry-signal_sl|` — no special-casing needed; the kernel and
`abs()` distances handle both.
