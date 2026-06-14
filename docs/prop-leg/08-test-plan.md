# 08 — Test Plan (write FIRST, per task)

TDD: each file written and watched fail before its module. Exact expected values given. Style mirrors the
reference `tests/layer2/`.

## §1 `test_phase2.py` (T2)
```python
# SHORT breakout-fade, EURUSD k=100000
g = compute_phase2(ticker="EURUSD", signal="SHORT",
    entry=1.08500, sl=1.08554, tp=1.08300, price_digits=5,
    contract_size=100000.0, tick_size=0.00001, tick_value=1.0,
    baseline_equity=100000.0, risk_pct=0.01)
assert g["lots"] == 18.52        # 1000 / (0.00054*100000)=1000/54
assert g["out_sl"] == 1.08554
assert g["out_tp"] == 1.08300
assert g["direction"] == "SHORT"
assert g["dollar_risk"] == 1000.0
assert round(g["target_reward"],0) == 3704   # RR≈3.70
# LONG case (entry 1.08500, sl 1.08446 tight/below, tp 1.08700 far/above) → symmetric
# zero stop (sl==entry) → {"reject"} ; max_lots cap (lots>max_lots) → {"reject"}
```

## §2 `test_phase1.py` + `test_stages.py` (T3)
```python
assert derive_stages(100000, 4500, 10.0, 3) == [104500.0, 107250.0, 110000.0]
assert active_stage_index([104500,107250,110000], 100000, 0) == 0   # ratchets only
assert active_stage_index([104500,107250,110000], 105000, 0) == 1
assert active_stage_index([104500,107250,110000], 99999, 1) == 1    # never below prev_index
# compute_phase1: reward_gap=active_stage-live; lots FIXED by fixed_risk; tp_distance=reward_gap/(lots*k)
#   XAU example: fixed_risk=1000, stop≈1000 ticks → lots=1.00, reward_gap=4500 → realized_RR=4.5
# rejects: reward_gap<=0 ; zero stop ; out_tp collapses onto entry/out_sl
# validate_phase1_inputs: first_reward<target, min_days>=2, all positive
# parse_reward_risk("9000:2000")==(9000.0,2000.0); malformed -> ValueError
```

## §3 `test_kills.py` + `test_buffers.py` (T4)
```python
# K1 dynamic: day_start=103000, daily_dd=2 → floor 100940
assert evaluate_phase2_kills(equity=100939, day_start=103000, baseline=100000, daily_dd=2,
        overall_dd=5, daily_cap=2.5, profit_target=10, ...)["kill"] == "K1"
# K2 static: baseline=100000, overall_dd=5 → floor 95000 (permanent)
assert evaluate_phase2_kills(equity=94999, baseline=100000, overall_dd=5, ...) == {"kill":"K2","permanent":True}
# K3 daily cap: day_start+baseline*2.5/100 → ceiling ; K4 profit target baseline*1.10=110000 (permanent)
# K5 consistency: largest_day/total < threshold AND >=2 profitable days (permanent)
# Phase 1 priority: K2 > K1 > stage-win > K4
# override: suppresses K1/K3/stage; does NOT suppress K2/K4/K5
# buffers: apply_buffers({daily:3,overall:5,profit_target:10,consistency:30}) ->
#   daily=2.0, overall=5.0, daily_profit_cap=2.5, consistency=29.0
```

## §4 `test_dayroll.py` (T5)
```python
# propfirm_day_roll="11:00" SGT
assert current_day(sgt("2026-06-14 10:30")) == date(2026,6,13)
assert current_day(sgt("2026-06-14 11:30")) == date(2026,6,14)
# currency auto from account_currency (e.g. account reports USD)
assert money(-12.5, "USD") == "$12.50"            # sign handled by signed-money helper
assert money(12.5, "EUR") == "EUR 12.50"          # auto, non-USD
# price: USDJPY 3dp, EURUSD 5dp, XAUUSD 2dp, XAGUSD 4dp
```

## §5 `test_webhook_validation.py` (T9)
Full 14-field → accepted; missing any field → 422; `signal="BUY"` → 422; unknown ticker → 422;
`entry=0`/`sl=-1` → 422; lowercase `signal` upper-cased.

## §6 `test_gate_chain.py` (T9) — mocked ZMQ + clock
Each gate in order → right outcome: curfew → reject; permanently_halted → blocked; not active → skipped;
news/manual suppress → suppressed; dedup (pair open) → dropped; max_open reached → skipped;
trade_allowed=False → blocked; baseline≤0 → blocked; geometry reject → reject; order_check reject →
"not placed"; **phase==1 routes to compute_phase1, else compute_phase2**; happy path → exactly ONE ticket
matching `05 §2`.

## §7 `test_messages.py` (T11)
Trade Opened renders the account currency (no `$+`/`$-`); Position Closed `found=False` → `(est.)`,
`found=True` → signed money sign-before-symbol; prices carry no symbol; each kill alert renders with its
phase context. Audit: a rendered alert contains no `$+`/`$-`.

## §8 Worker units (T7/T8)
filling-mode order IOC→FOK→RETURN; ticket→MT5 request mapping; fee `(balance−Σprofit)−anchor`;
deal window upper bound = now+1day; `deal_pnl` found=False without DEAL_ENTRY_OUT; force-close reason map
(daily→K1, overall→K2, cap→K3, target→K4, consistency→K5, stage→STAGE_REACHED); static-DD floor breach
fires the guard. Live MT5 → CP-2.

## §9 Kills-fire simulation (CP-1 deliverable, T12)
A scripted equity series that trips, in sequence on a fresh account: K1 (day loss), recovery + a stage
win + ratchet, K4 (funded), then in Phase 2 K3 (cap), K5 (consistency), and K2 (overall) — asserting the
correct halt + alert + permanence each time. This is the proof the challenge engine is correct end-to-end.

## Green bar
`pytest` all green + `dry_run_signal.py` correct + the §9 simulation ⇒ ready for CP-1.
