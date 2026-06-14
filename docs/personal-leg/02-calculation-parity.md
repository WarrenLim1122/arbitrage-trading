# 02 — Hedge Reconstruction (personal = inverse mirror of the prop trade)

Personal does **not** compute geometry from a raw signal. It **reconstructs its leg from the prop trade**
it reads off the Telegram group, using the original system's personal-leg relationship — verified verbatim
against `layer2/phase1_strategy.py` and `layer2/phase2_strategy.py`.

## §0 The relationship (true in BOTH phases — proven from the reference)
In the reference, for every trade the personal leg satisfies:
```
pers_signal = invert_signal(prop_signal)      # strategy_common.invert_signal: LONG<->SHORT
pers_lots   = round(prop_lots × phase_ratio, 2)   # phase_ratio = phase_multipliers[phase]
pers_sl     = prop_tp                          # personal SL price = prop TP price
pers_tp     = prop_sl                          # personal TP price = prop SL price
```
- **Phase 2** (`phase2_strategy.compute_geometry`): `prop_sl = signal_tp`, `prop_tp = signal_sl`;
  `pers_sl = signal_sl`, `pers_tp = signal_tp`. ⇒ `pers_sl = prop_tp`, `pers_tp = prop_sl`. ✓
- **Phase 1** (`phase1_strategy.compute_geometry:171-172`): `pers_sl = prop_tp` (the calculated far
  barrier), `pers_tp = prop_sl` (= signal TP near). ⇒ same identity. ✓
- **Lots** (both): `pers_lots = round(prop_lots × phase_ratio, 2)`. `phase_multipliers = {1: 0.20, 2: 0.70}`.

So personal needs **only** these five facts from the prop's published trade: `pair`, `prop_signal`,
`prop_sl`, `prop_tp`, `prop_lots`, and `phase`. Everything else follows.

## §1 The reconstruction function
```
reconstruct_personal(*, pair, prop_signal, prop_sl, prop_tp, prop_lots, phase,
                     price_digits, phase_multipliers) -> dict:
    mult           = phase_multipliers[str(phase)]          # 1->0.20, 2->0.70
    pers_signal    = invert_signal(prop_signal)             # LONG<->SHORT
    pers_lots      = round(prop_lots × mult, 2)
    pers_sl        = round(prop_tp, price_digits)
    pers_tp        = round(prop_sl, price_digits)
    if pers_lots <= 0: return {"reject": "personal lots round to 0"}
    return {"ticker": pair, "signal": pers_signal, "lots": pers_lots,
            "sl": pers_sl, "tp": pers_tp}
```
Entry: personal sends a **market** order on the pair (it acts when the prop alert arrives, slightly after
the prop fill — a small, accepted hedge lag). The `sl`/`tp` are the prop's TP/SL prices (the mirror box).

## §2 Worked example (mirror of the prop's breakout-fade SHORT)
Prop publishes (its own Trade Opened alert, Phase 2): `pair=EURUSD, prop_signal=SHORT,
prop_entry=1.08500, prop_sl=1.08554, prop_tp=1.08300, prop_lots=18.52, phase=2`.
```
mult        = 0.70
pers_signal = invert(SHORT) = LONG
pers_lots   = round(18.52 × 0.70, 2) = 12.96
pers_sl     = prop_tp = 1.08300
pers_tp     = prop_sl = 1.08554
```
Result: personal **LONG** EURUSD, 12.96 lots, SL 1.08300 (far/below), TP 1.08554 (near/above) — i.e. the
original Layer-0 personal profile (RR ≈ 0.27), the exact inverse of the prop. Net exposure across both
accounts = the original coupled hedge. ✓

Phase 1 example: prop publishes `prop_lots=1.00, phase=1` → `pers_lots = round(1.00 × 0.20, 2) = 0.20`;
`pers_sl=prop_tp`, `pers_tp=prop_sl`, `pers_signal=invert(prop_signal)`.

## §3 First regression test (write before implementing — `tests/test_reconstruction.py`)
The §2 Phase-2 case must return `signal="LONG"`, `lots==12.96`, `sl==1.08300`, `tp==1.08554`,
`ticker=="EURUSD"`. A Phase-1 case with `prop_lots=1.00,phase=1` → `lots==0.20`. A `prop_lots` so small
that `×mult` rounds to 0 → `{"reject"}`. A prop LONG → personal SHORT (symmetry).

## §4 Why no native sizing
The personal account has no independent sizing anchor in this model — by design it **inherits** the
prop's sizing (`prop_lots × phase_mult`), exactly as the original coupled system did. Personal live equity
is used only for reporting and (optionally) a secondary protective DD halt — never for sizing.
