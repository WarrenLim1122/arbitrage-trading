# Phase 1 Strategy — Design Spec

**Date:** 2026-05-16
**Status:** Awaiting Warren's review (no code written yet)
**Scope:** Layer 2 only. Layer 0/1/3 unchanged except where explicitly noted.

---

## 1. Goal

Today Phase 1 and Phase 2 share one execution path: both use the Layer 0 signal's
TP/SL geometry (RR ≈ 0.27 baked into Pine), size the prop leg at
`baseline_equity × 0.67%`, and differ only by the personal-account lot multiplier
(Phase 1 = 0.20, Phase 2 = 0.70).

Phase 2 must keep behaving **exactly** as it does now.

Phase 1 changes to a **dynamic reward-targeting strategy**. The prop account is
deliberately driven toward either the funded line ($110k) or a controlled bust
($94k), while the real-money personal account holds the inverse hedge. Reward per
trade is dynamic; risk per trade is fixed. There is no Layer-0 RR dependence in
Phase 1.

This spec separates the two phases into their own modules so Phase 1's new risk
logic cannot regress Phase 2's live funded behavior.

---

## 2. Architecture — file split

`layer2/logic_core.py` is ~1,609 lines and monolithic. The natural seam is the
phase branch already present in `receive_signal()` and `_run_equity_check()`.

| File | Responsibility |
|---|---|
| `layer2/logic_core.py` | **Orchestrator.** FastAPI `/signal` endpoint, 30 s equity-monitor loop, news-preclose loop, close/mismatch detection, ZMQ dispatch, shared helpers, `SignalPayload`. Reads `_phase_state["phase"]` and **delegates** sizing/geometry/kill decisions to the active phase module. |
| `layer2/phase2_strategy.py` | **Phase 2 — verbatim extraction.** Current 0.67% prop sizing, ×0.70 personal multiplier, signal-driven inverse-swap TP/SL, kills K1–K5 incl. consistency. No behavioral change. |
| `layer2/phase1_strategy.py` | **Phase 1 — new.** Active-stage ratchet, dynamic reward targeting, fixed-$R prop sizing, prop-TP/SL + personal-TP override, Phase 1 kill set, day-lock-on-stage-win. |

**Seam contract.** Both strategy modules expose the same two pure-ish functions
the orchestrator calls:

```
size_and_geometry(payload, propfirm_cfg, phase_state, live_prop_equity)
    -> { prop_ticket_fields, pers_ticket_fields, log_fields }

evaluate_kills(propfirm_cfg, phase_state, live_prop_equity, day_start_equity,
               consistency_log, now_sgt)
    -> None | { reason, halt: bool, permanent: bool, telegram_msg }
```

The orchestrator keeps owning ZMQ I/O, MT5 equity polling, Telegram transport,
and state persistence. Strategy modules compute decisions; they do not perform
side effects directly (testable in isolation, and the orchestrator stays the one
place that talks to the outside world).

Phase 2 extraction is a **pure refactor**: extract, wire the dispatcher, then
diff the produced ticket/kill dicts against the pre-refactor code for an
identical set of recorded signals before any Phase 1 work begins (see §13).

---

## 3. Phase 1 trade geometry (LOCKED)

There is **one anchor price**: the Layer 0 signal's **SL price**. It serves as
**personal's SL** and **prop's TP** simultaneously (opposite directions, same
price level). The other two prices — prop SL and personal TP — are computed.

### Worked example

EURUSD, signal **LONG**, entry `1.08500`, signal SL `1.08300`
→ signal SL distance **D = 20 pips**.
First trade: prop equity `$100,000`, Stage 1 = `$109,000`
→ reward gap = `$9,000`; fixed risk **R = $2,000**.

| | Dir | SL | TP | Lots | Risk | Reward |
|---|---|---|---|---|---|---|
| **Prop** (inverse) | SHORT | `1.085444` *(computed, ≈4.44 pips)* | **`1.08300` = signal SL** | 45 | **$2,000** (fixed R) | **$9,000** (stage gap) |
| **Personal** (signal) | LONG | **`1.08300` = signal SL** | `1.085444` *(computed)* | 9 (= 0.2 × prop) | **$1,800** | **$400** |

Both legs share both price levels → they close together (clean mirror).

### Per-trade computation

1. From Layer 0 payload: signal SL distance **D**, entry, direction.
2. `reward$_prop = active_stage − live_prop_equity` (e.g. `$9,000`).
3. `lots_prop = reward$_prop / (D × prop_tick_value)` — prop TP anchored at the
   signal-SL price, so this lot size makes the prop win exactly the stage gap.
4. `prop_SL_distance = R / (lots_prop × prop_tick_value)` → prop risk = exactly
   **R** (fixed). Prop SL is on the side opposite the anchor.
5. `lots_personal = 0.2 × lots_prop`. Personal SL = signal SL price (signal
   direction). Personal TP = prop SL price.
   → `personal_risk = 0.2 × reward$_prop` (dynamic), `personal_reward = 0.2 × R`
   (fixed = $400 when R = $2,000).

### Fixed vs dynamic

| Quantity | Fixed / Dynamic |
|---|---|
| Prop risk | **Fixed** = R every trade |
| Personal reward | **Fixed** = 0.2 × R every trade |
| Prop reward | **Dynamic** = `active_stage − live_prop_equity` |
| Personal risk | **Dynamic** = `0.2 × prop reward` (escalates as RR hardens) |

### Direction mapping

`signal LONG` → personal LONG, prop SHORT (and the mirror for `signal SHORT`).
Prop's TP is always at the signal-SL price; prop's SL is always the computed
price on the opposite side; personal mirrors prop's two prices.

### Invariants & cross-broker note

The exact invariants are: **shared price anchors** (personal SL = prop TP =
signal SL; personal TP = prop SL) and **`lots_personal = 0.2 × lots_prop`**.
Dollar figures ($1,800 / $400) hold when both brokers' tick values match;
Fusion vs FundingPips contract/tick differences make the dollar mirror
*indicative, not exact*. Implementation uses each worker's real instrument spec
(the existing per-broker tick-value path), with the lot ratio and price anchors
as the controlled invariants.

### Accepted risk profile

Personal real-money per-trade risk = `0.2 × prop reward`, uncapped. Near a
losing streak (equity $95k, Stage $109k → prop reward $14k) personal risks
≈ **$2,800 real to make $400**. Warren explicitly accepts this uncapped: it is a
two-way hedge and any drawdown is recoverable in Phase 2 profit-sharing. **Not a
bug — design intent.**

---

## 4. Active-stage ratchet (LOCKED)

Stages are **cumulative absolute prop-equity targets**, not per-trade profits.

- `stages = [S1, S2, …, Sn]`, ascending, with `Sn = baseline + overall_target`.
- `active_stage_index` starts at the **lowest stage strictly greater than current
  prop equity**.
- The index **only advances, never reverts**. After any equity update, advance
  it past every stage the live equity now meets or exceeds.
- Per-trade reward = `stages[active_stage_index] − live_prop_equity`.
- Reaching the **final** stage (`Sn = baseline + overall_target`) = K4 profit
  target → permanent halt → `/phase2`.

Best-case path (no losses), example R=$2k, Stage targets 109/109.5/110k:

| Trade | Equity before | Active stage | Reward this trade |
|---|---|---|---|
| 1 | $100,000 | $109,000 | $9,000 → hits Stage 1 |
| 2 | $109,000 | $109,500 | $500 → hits Stage 2 |
| 3 | $109,500 | $110,000 | $500 → hits Stage 3 (funded) |

After a loss, equity drops, the active stage stays put, so the next reward is
recomputed larger (matches Warren's Case 2 & Case 3). Because the reward uses
**live equity — which already reflects spread/commission paid on losses** — the
next target automatically absorbs (recovers) that cost.

`active_stage_index` is **persisted** so a process restart never resets the
ratchet (see §12).

---

## 5. Stage derivation & `/phase1` wizard (LOCKED)

### Inputs

`/phase1` asks for **one value**: the first-trade `reward:risk` in dollars,
e.g. `9000:2000`.

- `reward` part → **W1** (first-trade reward) → `Stage 1 = baseline + W1`.
- `risk` part → **R** = fixed dollar risk for **every** Phase 1 trade
  (lots sized so a prop loss = exactly R; "average" was loose wording — it is a
  hard fixed per-trade risk).

### From existing prop-firm config (NOT re-entered in `/phase1`)

Set via `/changepropfirm`, read from `propfirm_config.json`:

- `overall_target` = `baseline × profit_target_pct / 100` (e.g. $10,000)
- `overall_stop`   = `baseline × max_drawdown_overall_pct / 100` (e.g. $6,000)
- `min_profit_days` = number of stages (NOT hardcoded — e.g. 3 → 3 stages)

### Derivation

```
W1     = reward part of input
R      = risk part of input
n      = min_profit_days                       (from propfirm_config)
target = baseline × profit_target_pct / 100    (from propfirm_config)
step   = (target − W1) / (n − 1)

stages = [ baseline + W1,
           baseline + W1 + step,
           …,
           baseline + target ]                 # length n, last = baseline+target
```

Example: W1=$9,000, target=$10,000, n=3 → step=$500 → **$109,000 / $109,500 /
$110,000**. If `min_profit_days = 4` → `÷ 3` → 4 stages.

### Validation (wizard rejects with a clear message)

- Input must match `^\s*\d+(\.\d+)?\s*:\s*\d+(\.\d+)?\s*$`.
- `R > 0`, `W1 > 0`.
- `W1 < target` (else no room for stages 2…n).
- `n ≥ 2` (need at least Stage 1 + funded line).
- `R` should be < the Phase 1 daily-DD amount (else the first loss instantly
  trips K1). If `R ≥ daily_DD_amount`, the recap shows a ⚠️ line stating only
  one losing trade fits per day — informational, not a hard block.

### Telegram messages (final wording — Warren's cuts integrated)

Prompt:

```
⚙️  Phase 1 Setup

Send first-trade  reward:risk  (in $)
   e.g.   9000:2000

• Reward — profit target of your FIRST Phase 1
  trade (sets Stage 1 = baseline + this).
• Risk — fixed $ lost if any trade hits SL.
  Identical for every trade.

ℹ️ Remaining stages are spread automatically:
   (overall target − first reward) ÷ (min profitable days − 1)
```

Recap after a valid input:

```
✅  Phase 1 Ready

First reward : $9,000  → Stage 1 = $109,000
Fixed risk   : $2,000   (every trade)
Stages       : $109,000 → $109,500 → $110,000
Overall stop / target : $94,000 / $110,000

Reply CONFIRM to proceed.
Send /cancel to abort.
```

Format matches existing wizards: HTML `parse_mode`, `<code>CONFIRM</code>`, the
two-line `Reply CONFIRM to proceed.` / `Send /cancel to abort.` convention
(telegram_handlers.py:1181–1182, 1533–1534). New conversation states added
alongside `PF_*` / `P2_*`; baseline immutability rules unchanged — `/phase1`
still only locks `baseline_equity` when it is ≤ 0 (idempotent, as today at
telegram_handlers.py:575–642).

---

## 6. Day model (LOCKED) — halt on stage-win

- Trades run with fixed risk R and dynamic reward.
- The **first trade that wins** lands exactly on the active stage. On that win:
  1. record a profitable day (counter, informational),
  2. **halt the prop account for the rest of the SGT day**,
  3. ratchet `active_stage_index` forward,
  4. auto-resume next SGT session (reuse the existing `daily_halted` +
     `daily_halted_date` + 11:00 SGT rollover mechanism, with a new reason
     `phase1_stage_reached`).
- A **losing** trade closes at −R. Losses accumulate within the day until either
  a winning trade hits the stage (→ halt as above) or K1 daily-DD halts the day.
- The structural "≥ `min_profit_days` profitable days" guarantee is **emergent**:
  there are `min_profit_days` stages, each stage-hit halts the day, so reaching
  the funded line necessarily spans ≥ `min_profit_days` winning days. No separate
  day-counting gate is needed; the counter is display-only.

This replaces Phase 2's K3 config-% daily profit cap. Phase 1 has **no K3**; the
"daily profit ceiling" is dynamically the active stage itself ("the profit kill
rule is a bit different from Phase 2").

---

## 7. Kill conditions in Phase 1

| Kill | Phase 1 | Definition in Phase 1 |
|---|---|---|
| K1 — daily loss | **ON** | `equity ≤ day_start − (day_start × dd_daily_pct_effective/100)`. Buffer change in §8. Force-close both legs + daily halt, auto-resume next session. |
| K2 — overall loss | **ON** | `equity ≤ baseline − overall_stop` (e.g. $94,000). Force-close both + **permanent** halt. This is the accepted "controlled bust". |
| K3 — daily profit cap | **OFF** | Replaced by stage-hit day-halt (§6). |
| K4 — profit target | **ON** | `equity ≥ baseline + overall_target` (= final stage Sn = $110,000). Force-close both + permanent halt → `/phase2`. |
| K5 — consistency | **OFF** | Phase 2 only. |

News filter (Layer 1) and news-preclose (Layer 2) are **disabled while
phase == 1** — evaluation phase has no funded-account news rule exposure. Memory
`phase1-news-filter-future` records that Warren may later want Phase 1 to reuse
Phase 2's "NO NEWS" logic; wiring deferred. Implementation note: news gating is
in the Layer 1 gatekeeper (phase-agnostic today); making it phase-aware is the
deferred task — for now Phase 1 simply does not apply it. Exact disable
mechanism (Layer 1 reads phase vs Layer 2 bypass) is an implementation-plan
decision; default recommendation: Layer 2 skips the news-preclose loop when
phase == 1 and the orchestrator does not reject on the Layer 1 news verdict in
Phase 1, leaving Layer 1 itself untouched.

---

## 8. Daily-DD buffer change (LOCKED) — affects BOTH phases

`state.py` `_apply_buffers()` (≈ lines 250–263) currently does
`max_drawdown_daily_pct_effective = raw − 1.0`. Change the daily buffer from
**−1.0 pp to −0.5 pp** for **both** Phase 1 and Phase 2 (−1.0 pp was too tight
under real spread + commission; −0.5 pp is the stated max tolerance). No
two-tier. The consistency-rule buffer stays **−1.0 pp** (untouched).

This is the only change in this spec that touches live Phase 2 behavior. It is
intentional and approved. It must be called out in the implementation plan as a
Phase 2-affecting change and verified separately.

---

## 9. Per-signal flow (Phase 1)

```
Layer 0 webhook → Layer 1 (no news gate in Phase 1) → Layer 2 /signal
  → orchestrator reads phase == 1
  → query live prop equity (fresh MT5 query; account is flat between trades
    because the day halts on a win and K1 halts on a loss)
  → phase1_strategy.size_and_geometry():
       active_stage   = stages[active_stage_index]
       reward$_prop   = active_stage − live_prop_equity
       D              = |entry − signal_sl|
       lots_prop      = reward$_prop / (D × prop_tick_value)
       prop_sl_dist   = R / (lots_prop × prop_tick_value)
       prop:  dir = invert(signal); TP = signal_sl price; SL = computed
       pers:  dir = signal;         SL = signal_sl price; TP = prop SL price
       lots_pers      = 0.2 × lots_prop
  → orchestrator builds prop_ticket / pers_ticket, dispatches via ZMQ
  → existing verify-and-notify path (Telegram alert)
```

Equity source: **live prop MT5 equity at signal arrival** (the value already
includes realized spread/commission, which is what gives the automatic
spread-recovery property in §4).

---

## 10. Telegram alert implications

`Trade Opened` / `Trade Closed` alerts must, in Phase 1, show the **dynamic**
figures rather than the Layer 0 RR:

- active stage & target, reward$ this trade, RR this trade (`reward$ / R`),
- prop fixed risk R, personal risk (dynamic) & personal reward (fixed),
- on close: which stage (if any) was reached, profitable-day count.

Phase 2 alert format unchanged. Existing pending close-alert P&L breakdown work
(net/gross/commission) is **out of scope here** — tracked separately, do not
fold in.

---

## 11. State & config schema

### `propfirm_config.json` — read-only here (no schema change)

Phase 1 consumes existing fields: `baseline_equity`, `profit_target_pct`,
`max_drawdown_overall_pct`, `max_drawdown_daily_pct`, `min_profit_days`,
`pers_baseline_equity` (display only). No new fields; no change to
baseline-immutability rules.

### New Phase 1 strategy state

Persisted (survives restart) — extend `phase_config.json` (or a sibling
`phase1_state.json`; implementation-plan decision, default: nested block in
`phase_config.json` to keep one phase-state file):

```jsonc
"phase1": {
  "first_reward": 9000.0,        // W1, from /phase1 input
  "fixed_risk": 2000.0,          // R, from /phase1 input
  "stages": [109000.0, 109500.0, 110000.0],  // derived at /phase1 confirm
  "active_stage_index": 0,       // ratchet pointer, only increments
  "profitable_days": 0,          // display-only counter
  "last_stage_day": "never"      // SGT date a stage was last reached
}
```

`stages` is frozen at `/phase1` confirm time from W1 + R + propfirm_config.
Re-running `/phase1` recomputes it (and re-locks baseline only if ≤ 0).
Existing `daily_halted` / `daily_halted_date` reused for the stage-win day-halt
with reason `phase1_stage_reached`.

---

## 12. Edge cases & validation

1. **Lot explosion.** Tight D + large reward gap → very large `lots_prop`
   (example: 45 lots). If computed lots exceed the instrument's broker max-lot
   or available margin: **reject the trade + Telegram alert** (do NOT silently
   clamp — clamping breaks the exact-reward invariant and silently changes risk).
   This is a hard requirement; surfaced as a first-class Phase 1 alert.
2. **Spread/commission shortfall on a win.** A "win" may land slightly below the
   stage after costs. Rule: a stage is "reached" only when
   `live_equity ≥ stage`. A small shortfall → not advanced; next trade's reward
   = the small remaining gap (self-healing, recovers the cost).
3. **Equity already above S1 at `/phase1`.** `active_stage_index` initialized to
   the lowest stage strictly greater than current equity (skips already-passed
   stages).
4. **Overshoot on a single win.** After any equity update, advance index past
   every stage now met/exceeded (not just one).
5. **`R ≥ daily_DD_amount`.** Allowed but flagged in the recap (only one losing
   trade fits/day).
6. **`W1 ≥ target`.** Hard wizard rejection.
7. **`min_profit_days < 2`.** Hard wizard rejection (need Stage 1 + funded).
8. **Restart mid-evaluation.** `phase1` block (incl. `active_stage_index`)
   persisted → ratchet and stages survive a Layer 2 restart.
9. **No open positions assumption.** Day halts on win, K1 on loss → account is
   flat between signals, so equity-at-signal is a clean basis. If a position is
   somehow open when a new signal arrives (mismatch/manual), the existing
   mismatch detection path applies before sizing.
10. **D = 0 / missing signal SL.** Reject the signal (cannot size); alert.

---

## 13. Phase 2 regression safety

Phase 2 extraction is a **no-op refactor**. Verification before any Phase 1
code:

- Capture a set of recorded historical `SignalPayload`s + `propfirm_config` +
  `phase_state` (phase = 2).
- Run them through the pre-refactor `receive_signal()` / `_run_equity_check()`
  and the post-refactor `phase2_strategy` path.
- Assert the produced `prop_ticket`, `pers_ticket`, and kill decisions are
  **byte-identical** (modulo timestamps/ids).
- Only then start Phase 1 work.

The −0.5 pp daily-buffer change (§8) is the single intentional Phase 2 behavior
change and is verified explicitly (recompute K1 floors for a sample
`propfirm_config`, confirm new floor = `day_start − day_start × (raw−0.5)/100`).

---

## 14. Testing strategy

- **Unit:** `phase1_strategy.size_and_geometry()` against Warren's Case 1
  (clean win path 9k/500/500), Case 2 (win then losses → growing RR), Case 3
  (pure loss path → bust at $94k). Assert stages, reward$, RR, lots, the
  prop-SL/personal-TP prices, and the price-anchor equality each step.
- **Unit:** `phase1_strategy.evaluate_kills()` — K1 (with −0.5 pp), K2 $94k,
  K4 $110k; assert K3/K5 never fire in Phase 1.
- **Unit:** stage derivation for `min_profit_days` ∈ {2,3,4}; wizard input
  parsing + every validation/rejection branch.
- **Unit:** ratchet — never reverts; overshoot skip; restart persistence.
- **Regression:** §13 Phase 2 equivalence harness.
- **Dry run:** demo accounts, walk a full Stage 1→2→3 sequence and a bust
  sequence, confirm day-halt/auto-resume and Telegram alerts, before any live
  capital.

---

## 15. Deployment

Layer 2-only change → `/update layer2` on VPS #1 after merge. `uv sync` only if
`pyproject.toml` changes (it should not). Demo-first mandatory: walk Cases 1–3
on demo before switching to live Fusion/FundingPips. Per repo workflow:
auto-commit + push to `main`; then Warren runs `/update layer2`.

---

## 16. Out of scope / future

- Re-wiring Phase 1 to reuse Phase 2's "NO NEWS" logic (memory
  `phase1-news-filter-future`).
- Two-tier (warn + halt) daily-DD buffer (rejected for now; flat −0.5 pp).
- Close-alert net/gross/commission P&L breakdown (separate pending task).
- Any Layer 0/1/3 change beyond the Phase 1 news-skip wiring in §7.

---

## 17. Open implementation-plan decisions (not requirements gaps)

These are settled in principle; the implementation plan picks the mechanism:

- Phase 1 state location: nested `phase1` block in `phase_config.json`
  (recommended) vs separate `phase1_state.json`.
- News disable mechanism in Phase 1: Layer 2 skip (recommended) vs Layer 1
  phase-aware.
- Strategy-module seam exact signatures (kept pure; orchestrator owns I/O).

All functional requirements above are LOCKED per Warren's answers
(2026-05-16 brainstorming session).
