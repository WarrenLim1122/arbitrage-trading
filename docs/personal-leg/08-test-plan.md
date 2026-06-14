# 08 — Test Plan (write these FIRST, per task)

TDD: each test file below is written and watched fail **before** the matching module is implemented.
Exact expected values are given so there is zero ambiguity. Mirror the style of the reference test
`tests/layer2/test_phase2_strategy.py`.

## §1 `test_geometry.py` (T2) — the pinned kernel
Inputs reused from `02-calculation-parity.md §3`. EURUSD ends "USD" → `k = contract_size = 100000`.

```python
# LONG — the parity case
g = compute_personal_geometry(
    ticker="EURUSD", signal="LONG",
    entry=1.08500, signal_sl=1.08300, signal_tp=1.08554, price_digits=5,
    contract_size=100000.0, tick_size=0.00001, tick_value=1.0,
    personal_baseline=100000.0, risk_pct=0.01)
assert g["lots"]        == 5.00          # 1000 / (0.00200*100000)=1000/200
assert g["sl"]          == 1.08300
assert g["tp"]          == 1.08554
assert g["direction"]   == "LONG"
assert g["dollar_risk"] == 1000.0        # EXACTLY baseline*pct, constant
assert round(g["realized_rr"], 2) == 0.27

# SHORT — symmetric. entry 1.08500, signal_sl 1.08700 (above), signal_tp 1.08446 (below)
#   sl_distance=0.00200 → lots=5.00, direction="SHORT", sl=1.08700, tp=1.08446

# Reject — zero SL distance
g = compute_personal_geometry(..., entry=1.08500, signal_sl=1.08500, ...)
assert "reject" in g

# Reject — lots round to 0 (tiny baseline vs huge stop) and max_lots cap (lots>max_lots) both reject
```
Also test a non-USD pair (e.g. EURJPY: `ticker` not ending "USD" → `k = tick_value/tick_size`) to prove
the `dollar_per_unit` branch is threaded correctly.

## §2 `test_halts.py` (T4)
```python
# daily breach: day_start=100000, daily_pct=4 → floor 96000
assert evaluate_halts(equity=95999, day_start_equity=100000, baseline=100000,
                      daily_pct=4, overall_pct=8, override_active=False)["halt"] == "daily"
assert evaluate_halts(equity=96001, ...)["halt"] is None
# overall breach: baseline=100000, overall_pct=8 → floor 92000 (permanent)
assert evaluate_halts(equity=91999, day_start_equity=100000, baseline=100000,
                      daily_pct=4, overall_pct=8, override_active=False)["halt"] == "overall"
# override suppresses daily, NOT overall
assert evaluate_halts(equity=95999, ..., override_active=True)["halt"] is None
assert evaluate_halts(equity=91999, ..., override_active=True)["halt"] == "overall"
```

## §3 `test_dayroll.py` (T3)
```python
# day_roll="11:00" SGT. A signal at 10:30 SGT belongs to YESTERDAY's trading day; 11:30 to TODAY's.
assert current_day(sgt("2026-06-14 10:30")) == date(2026,6,13)
assert current_day(sgt("2026-06-14 11:30")) == date(2026,6,14)
# currency rendering
assert money(-12.5, "SGD") == "SGD 12.50"     # no '$'
assert money(12.5, "USD")  == "$12.50"
assert "$" not in money(12.5, "SGD")
# price formatting
assert fmt_price("USDJPY", 156.123) == "156.123"   # 3dp ; EURUSD 5dp ; XAUUSD 2dp ; XAGUSD 4dp
```

## §4 `test_webhook_validation.py` (T8)
- A full 14-field payload (all fields present, valid) → 200/accepted.
- Missing any one of the 14 fields → 422.
- `signal="BUY"` → 422; `ticker="FOOBAR"` (not in registry) → 422; `entry=0` / `sl=-1` → 422.
- `signal="long"` (lowercase) accepted and upper-cased to `LONG`.

## §5 `test_gate_chain.py` (T8) — mocked ZMQ + clock
Each gate, in order, produces the right outcome (assert the message builder called / HTTP status):
curfew → reject; `permanently_halted` → blocked; not `active` → skipped; news/manual suppress →
suppressed; **dedup** (pair already open) → dropped; `max_open_positions` reached → skipped;
`trade_allowed=False` → blocked; `personal_baseline<=0` → blocked; geometry reject → reject;
order_check reject → "not placed"; happy path → exactly ONE ticket pushed matching `05 §2`.

## §6 `test_messages.py` (T10) — format + currency invariants
- `msg_trade_opened(...currency="SGD"...)` contains `SGD ` on the risk row and **no** `$`.
- `msg_position_closed(found=False)` renders `(est.)`; `found=True` renders `+SGD ...` / `-SGD ...`
  (sign before symbol), never `$+`/`$-`.
- Entry/SL/TP rows contain a price with no currency symbol.
- Audit invariant test: scan a rendered personal alert for `$` → none present (account currency SGD).

## §7 Worker units (T6/T7) — where MT5 is mockable
- filling-mode selection order IOC→FOK→RETURN.
- ticket → MT5 request mapping (sl/tp/deviation/magic/volume) from a sample `05 §2` ticket.
- fee formula: `(balance − Σ deal.profit) − anchor`; deal-history upper bound == `now + 1 day`.
- `deal_pnl` returns `found=False` when no `DEAL_ENTRY_OUT` for the `position_id` is present.
- force-close reason mapping (daily→DAILY_DD, overall→OVERALL_DD).
Live MT5 integration (real connect/execute/journal) runs on the VPS at **CP-2**, not in CI.

## Green bar = T0–T11 done
`pytest` all green + the `scripts/dry_run_signal.py` trace correct ⇒ ready for CP-1.
