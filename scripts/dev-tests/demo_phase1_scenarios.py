"""DEMO: what Phase 1 does to a signal, run through the REAL strategy code.

Calls layer2/phase1_strategy.py UNCHANGED — derive_stages, active_stage_index,
compute_geometry, evaluate_kills — so the numbers below are exactly what the
live system would produce. Edit the CONFIG block to match your real /phase1 +
/changepropfirm settings.
Run:  uv run python scripts/dev-tests/demo_phase1_scenarios.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from layer2 import phase1_strategy as p1

# ── CONFIG (representative FundingPips $50k — confirm against your wizard) ──
BASELINE          = 50_000.0   # /changepropfirm baseline_equity (risk anchor)
PROFIT_TARGET_PCT = 10.0       # /changepropfirm — funded line = +$5,000 → $55,000
MIN_PROFIT_DAYS   = 5          # /changepropfirm — number of stages
FIRST_REWARD      = 4_500.0    # /phase1 "reward" half of 4500:1000
FIXED_RISK        = 1_000.0    # /phase1 "risk"   half of 4500:1000
MAX_PROP_LOTS     = 5.0        # config phase1.max_prop_lots (0 = unlimited)
PERS_RATIO        = 0.20       # PHASE_MULT[1]
DD_OVERALL_PCT    = 10.0       # K2 permanent floor
DD_DAILY_PCT      = 4.0        # K1 daily floor

# XAUUSD LONG signal: entry 2000.00, signal SL 1980.00 (D = 20.00). Gold:
# contract_size 100, tick 0.01/$1 → dollar_per_unit = 100 ($100 per 1.00 move/lot).
SIG = dict(ticker="XAUUSD", signal="LONG", entry=2000.00, signal_sl=1980.00,
           price_digits=2, prop_contract_size=100.0, prop_tick_size=0.01,
           prop_tick_value=1.0, pers_contract_size=100.0, pers_tick_size=0.01,
           pers_tick_value=1.0)

err = p1.validate_phase1_inputs(FIRST_REWARD, FIXED_RISK, BASELINE,
                                PROFIT_TARGET_PCT, MIN_PROFIT_DAYS)
print(f"validate_phase1_inputs -> {err or 'OK'}")
stages = p1.derive_stages(BASELINE, FIRST_REWARD, PROFIT_TARGET_PCT, MIN_PROFIT_DAYS)
print(f"derive_stages          -> {stages}")
print(f"  funded line (K4)     -> {stages[-1]:.2f}  (baseline + {PROFIT_TARGET_PCT}% )\n")


def run(label, live_equity, prev_idx=0, max_lots=MAX_PROP_LOTS):
    idx = p1.active_stage_index(stages, live_equity, prev_idx)
    print(f"── {label}")
    print(f"   live prop equity = {live_equity:,.2f}  → active stage idx={idx}", end="")
    if idx >= len(stages):
        print("  → FINAL STAGE reached (K4 funded / halt, no new trade)")
        return
    stage = stages[idx]
    print(f" (target {stage:,.2f}, reward gap {stage-live_equity:,.2f})")
    g = p1.compute_geometry(active_stage=stage, live_prop_equity=live_equity,
                            fixed_risk=FIXED_RISK, pers_ratio=PERS_RATIO,
                            max_prop_lots=max_lots, **SIG)
    if "reject" in g:
        print(f"   GEOMETRY REJECT → {g['reject']}  (signal placed = NO)\n")
        return
    print(f"   PROP  {g['prop_signal']:5s} {g['prop_lots']:>5.2f} lots | "
          f"SL {g['prop_sl']:.2f}  TP {g['prop_tp']:.2f} | "
          f"risk ${g['prop_dollar_risk']:,.2f}  reward ${g['prop_reward']:,.2f}  RR {g['prop_rr']:.2f}")
    print(f"   PERS  {g['pers_signal']:5s} {g['pers_lots']:>5.2f} lots | "
          f"SL {g['pers_sl']:.2f}  TP {g['pers_tp']:.2f} | "
          f"risk ${g['pers_dollar_risk']:,.2f}  reward ${g['pers_reward']:,.2f}")
    print(f"   (prop wins the stage gap; prop SL sized so a loss = fixed ${FIXED_RISK:,.0f}; "
          f"pers lots = {PERS_RATIO}× prop)\n")


print("================ SIGNAL SCENARIOS (XAUUSD LONG) ================\n")
run("1. Fresh start — first trade of the evaluation", 50_000.0)
run("2. After winning day 1 (equity at stage-1 line)", stages[0])
run("3. Running at a loss BELOW baseline (gap widens → lots grow)", 48_000.0)
run("4. Won most of the way (tiny reward gap → lots round to 0 = REJECT)", stages[-1] - 3.0,
    prev_idx=len(stages) - 1)
run("5. Big gap with a TIGHT max_prop_lots cap (=1.0) → REJECT", 48_000.0, max_lots=1.0)
run("6. Equity already past the funded line → FINAL/halt", stages[-1] + 10.0)

print("================ SAME SIGNAL, SHORT (direction flips) ================\n")
SIG_SHORT = {**SIG, "signal": "SHORT", "signal_sl": 2020.00}  # SHORT: SL above entry
g = p1.compute_geometry(active_stage=stages[0], live_prop_equity=50_000.0,
                        fixed_risk=FIXED_RISK, pers_ratio=PERS_RATIO,
                        max_prop_lots=MAX_PROP_LOTS, **SIG_SHORT)
print(f"   signal SHORT → PROP {g['prop_signal']} (inverse), PERS {g['pers_signal']} (follows)")
print(f"   PROP TP {g['prop_tp']:.2f} (=signal SL), SL {g['prop_sl']:.2f}; "
      f"PERS SL {g['pers_sl']:.2f} TP {g['pers_tp']:.2f}\n")

print("================ KILL EVALUATION (evaluate_kills) ================\n")
DAY_START = 50_000.0
# (equity, active_index, label) — active_index is the STORED ratchet index the
# day opened on (real logic keeps it in phase state; evaluate_kills halts the day
# when equity reaches stages[active_index]).
for eq, aidx, lbl in [
        (50_500.0, 0, "normal — no kill"),
        (DAY_START * (1 - DD_DAILY_PCT/100) - 1, 0, "hit daily floor (K1, resumes next session)"),
        (BASELINE * (1 - DD_OVERALL_PCT/100) - 1, 0, "hit overall floor (K2, PERMANENT)"),
        (stages[0] + 1, 0, "reached stage-0 target (day-halt, ratchets to stage 1)"),
        (stages[-1] + 1, len(stages) - 1, "reached funded line (K4, PERMANENT)")]:
    k = p1.evaluate_kills(prop_equity=eq, baseline=BASELINE, day_start=DAY_START,
                          dd_daily_pct=DD_DAILY_PCT, dd_overall_pct=DD_OVERALL_PCT,
                          stages=stages, active_index=aidx)
    print(f"   equity {eq:>10,.2f}  → {k if k else 'no kill (trading continues)'}   [{lbl}]")
