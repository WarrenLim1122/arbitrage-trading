# Master Plan — Standalone Personal-Account Leg

Status: **PLAN ONLY — not built.** Build is handed to a fresh Claude via `03-build-prompt.md`.
Author context: scoped 2026-06-14 with Warren. Calculation proof: `02-calculation-parity.md`.

---

## 1. Goal & guiding principle

Rebuild the **personal account leg** (Fusion Markets MT5, SGD) as a **standalone single-leg system**:
it trades the signal alone, with **no prop-firm hedge** and no FundingPips account anywhere in the loop.

**Guiding principle (Warren's instruction, verbatim intent):** the calculation logic and formulas must
**stay the same as the current system**, just expressed for a single leg. Today the *prop* leg holds the
authoritative, self-contained math and the *personal* leg is derived/parasitic. So we **reuse the prop's
calculation logic but apply it in the reverse (personal/signal-following) direction** — the opposite end of
the same SL/TP box. We do **not** invent a new strategy. See the worked proof in `02-calculation-parity.md`.

### What stays identical
- The risk kernel: `lots = risk_$ / (stop_distance × k)`, with `k` from `dollar_per_unit`
  (`layer2/strategy_common.py:13`) — the xxxUSD→`contract_size`, else `tick_value/tick_size` rule.
- Direction: personal **follows the signal** (LONG signal → LONG personal). This was always the personal
  side; the prop was the inverse. ([[signal-direction-is-personal]])
- Geometry: SL/TP come **straight from the signal** (`pers_sl = signal_sl`, `pers_tp = signal_tp`) — exactly
  what today's Phase 2 personal side does.
- Rounding to `price_digits`, the `tp/sl distance` definitions, the zero-distance reject guard.

### What changes (the only real re-design)
- **Sizing anchor becomes native.** Today `pers_lots = prop_lots × phase_multiplier`, where `prop_lots`
  come from `baseline_equity × 0.67%` (now 1%) over the **prop's** stop. With no prop, personal sizes
  **itself**: `risk_$ = personal_baseline × risk_pct`, over the **personal leg's own stop** distance.
- **Phases collapse.** Phase 1 (stage ladder) and Phase 2 (box) existed only to pass the prop-firm
  challenge. A personal account has no challenge → one unified geometry, with a risk-% **mode toggle**.
- **Personal gets risk halts** (it has none today — all kills K1–K5 were prop-only).

---

## 2. Resolved design decisions (locked with Warren 2026-06-14)

| # | Fork | Decision |
|---|---|---|
| 1 | Sizing anchor | **% of a fixed personal baseline**, immutable, set via Telegram (same pattern as today's prop `baseline_equity`). Not live-equity, not flat-$. |
| 2 | Phases | **Dropped.** Replaced by a **two-mode toggle** where the modes differ by **risk % only** (identical geometry in both). |
| 3 | SL/TP geometry | **Raw signal SL + TP**, computed as the prop's logic in the reverse (personal) direction. |
| 4 | Risk halts | **Daily + overall drawdown halt** on personal equity — mirror of prop K1/K2 (daily resets each session, overall is permanent). |
| 5 | Architecture | **Greenfield 2-service:** Linux **Receiver** + Windows MT5 **Worker**. Reuse Layer 0 Pine, symbol mapper, journaling, MT5 self-launch rule, transport. |
| 6 | Build flow | Plan + build-prompt only in this folder; a separate Claude builds it. |

---

## 3. Native sizing & geometry (the standalone math)

Pure function, single leg. All inputs config- or signal-driven; nothing hardcoded.

```
INPUTS:  signal (LONG/SHORT), entry, signal_sl, signal_tp, price_digits,
         contract_size, tick_size, tick_value      # live from MT5
         personal_baseline, risk_pct               # from config (Telegram-set; risk_pct from active mode)

k            = dollar_per_unit(ticker, contract_size, tick_size, tick_value)   # UNCHANGED kernel
sl_distance  = abs(entry - signal_sl)              # the personal leg's OWN stop (the wide/far side)
tp_distance  = abs(signal_tp - entry)              # personal target distance (the near side)
              # reject if sl_distance <= 0  (and tp_distance <= 0)

risk_$       = personal_baseline * risk_pct        # NATIVE anchor (replaces prop_lots × multiplier)
dollar_per_lot = sl_distance * k                   # if xxxUSD: k=contract_size; else tick math (same as prop)
lots         = round(risk_$ / dollar_per_lot, 2)   # reject if 0 or > max_lots
              # OPTIONAL: cap at max_lots (config), mirror of prop's max_prop_lots guard

direction    = signal                              # follow the signal (NOT inverted)
sl           = round(signal_sl, price_digits)
tp           = round(signal_tp, price_digits)
realized_RR  = tp_distance / sl_distance           # display only (≈ 0.27 for the current signal shape)
```

**Why this is "prop logic, reversed":** the prop sizes its risk over the **near** distance
(`|signal_tp − entry|`, because the prop's stop = signal_tp) and takes the **inverse** direction.
The personal leg sizes the same risk-$ over the **far** distance (`|entry − signal_sl|`, because the
personal's stop = signal_sl) and takes the **signal** direction. Identical formula, opposite end of the box.

**Consequence vs today:** the standalone risks **exactly** `personal_baseline × risk_pct` on every trade
(constant). Today's derived personal risk **floats** at `baseline × prop_pct × phase_mult × (sl_dist/tp_dist)`
(≈ 2.6% of baseline at the current ~0.27-RR signal shape). This is strictly better risk control. See
`02-calculation-parity.md` for the numbers.

---

## 4. Two-mode toggle (risk % only)

No prop challenge, so no Phase 1/2. Instead, two named risk profiles that **only swap `risk_pct`** — the
geometry, direction, kernel, and halts are identical in both. Telegram-switchable, persisted in config.

| Mode | `risk_pct` (default — **confirm**) | Use |
|---|---|---|
| `conservative` | 1.0% | Normal trading. |
| `aggressive` | 2.0% | Faster turnover when Warren wants it. |

- `/mode conservative` / `/mode aggressive` (or `/mode` to show current).
- Active mode's `risk_pct` is the only value that feeds sizing. Switching takes effect on the **next** signal.
- Both percentages are config fields (`personal_config.json`), editable via `/setrisk <mode> <pct>`.

> **Open — confirm the two percentages.** Defaults above are placeholders. To replicate today's *effective*
> personal risk (~2.6% of baseline) pick that; or choose deliberately for the standalone.

---

## 5. Risk halts (personal equity)

Mirror of the prop K1/K2 logic (`docs/reference/calculations.md` §Phase 2+), now on **personal** equity.
Personal had none before — this is new. Evaluated by the Receiver's equity monitor (polls the Worker).

| Halt | Condition | Permanent? |
|---|---|---|
| **Daily DD** | `pers_equity ≤ day_start_equity − day_start_equity × daily_dd_pct/100` | No — day halt, auto-resumes at the next SGT session roll. |
| **Overall DD** | `pers_equity ≤ personal_baseline − personal_baseline × overall_dd_pct/100` | **Yes** — permanent halt until cleared via Telegram. |

- `day_start_equity` snapshots at the SGT day roll (reuse `state._propfirm_day` logic / `propfirm_day_roll`).
- Telegram: `/halts` to show, `/setdailydd <pct>`, `/setoveralldd <pct>`, `/resume` (clear day halt),
  `/rearm`. Permanent halt cleared by an explicit `/clearhalt` (or re-running setup).
- No K3 (profit cap), no K5 (consistency) — those were prop-firm-specific. Optionally a daily-profit
  stop later if Warren wants; not in scope now.

> **Open — confirm `daily_dd_pct` and `overall_dd_pct`.** Suggested defaults: daily 4%, overall 8%.
> These are personal-account choices, set via Telegram; the plan does not hardcode them.

### 5a. Prop-halt listener (NEW — additional safety input) → full spec in `10-prop-halt-listener.md`
Beyond its own DD halts, the personal system **listens to the prop bot's K1–K5 kill/halt alerts** in a
shared Telegram group and closes/halts the matching position when the prop side stops. One-way, loose
coupling (the prop system is untouched and unaware) — works exactly like the news filter, but the trigger
is the prop bot's alert text. Build task **T8.5**. Defaults (CONFIRM at CP-1): pair-named kill → close
that pair; account-wide kill → close all + halt.

---

## 6. Architecture — greenfield 2-service

Minimum two processes regardless (public HTTPS receiver + a Windows MT5 worker). Clean rebuild, no
prop/hedge code.

```
TradingView (15m, one chart per pair)  ── Layer 0 Pine (REUSE, frozen) ──┐
                                                          HTTPS webhook   │
  ┌───────────────────────────────────────────────────────────────────── ▼ ──────┐
  │ RECEIVER  (Linux VPS, systemd, public TLS)                                     │
  │  • /signal endpoint (FastAPI) + 14-field webhook validation                    │
  │  • news filter + SGT curfew/trading-window gate   (REUSE layer1 logic)         │
  │  • per-pair dedup (multiple indicators fire same pair)  [[multi-indicator-dedup]]│
  │  • max-open-positions gate (counts PERSONAL positions now, not prop)           │
  │  • native sizing + geometry (§3)                                               │
  │  • risk-halt monitor (§5), day-roll, auto-resume                               │
  │  • Telegram bot (commands + all alert text)                                    │
  │  • ZMQ PUSH ticket / REQ query  ──────────────────────────────┐                │
  └────────────────────────────────────────────────────────────── │ ──────────────┘
                                                       ZMQ :5555/:5556
  ┌────────────────────────────────────────────────── ▼ ──────────────────────────┐
  │ WORKER  (Windows VPS, PowerShell)                                              │
  │  • MT5 self-launch + hard account guard (login == configured)                  │
  │  • PULL execute order (retry/limit fallback), REP query (equity/contract/...)  │
  │  • position-close watcher → journaling pipeline (REUSE layer3/journal)         │
  │  • symbol_mapper (REUSE) per-broker discovery + cache                          │
  └────────────────────────────────────────────────────────────────────────────────┘
```

- This **collapses today's Layer 1 + Layer 2 into one Receiver** (one account, no second leg to
  coordinate, no pre-flight dual-leg orphan problem) and keeps **one Worker** (the personal worker).
- All the prop-only machinery is **gone**: prop worker, baseline-as-prop-anchor, phase strategies,
  stage ladder, consistency log, K3/K4/K5, the inverse-direction leg, dual-leg pre-flight.

### Reuse inventory (lift, don't rewrite) — exact paths in `03-build-prompt.md`
- `layer0/1D-15m Breakout INDICATOR.pine` — signal engine (frozen; same 14-field webhook contract).
- `layer1/news_filter.py`, `layer1/ff_calendar.py` — news suppression.
- `layer2/symbols.py` + `config/symbols.json` — canonical pair registry.
- `layer2/strategy_common.py` — `dollar_per_unit`, `invert_signal` (kernel; keep `dollar_per_unit` as-is).
- `layer3/symbol_mapper.py` — per-broker symbol discovery + cache.
- `layer3/journal/` — journaling pipeline (Firestore). [[firestore-journal-verification]]
- MT5 self-launch + hard account-guard pattern from `layer3/_worker_core.py`. [[mt5-python-integration-constraints]]
- SGT day-roll / trading-window logic from `layer2/state.py` (`_propfirm_day`, `propfirm_day_roll`).
- Telegram message-formatting standards. [[telegram-reporting-standards]]

---

## 7. Config & Telegram surface

**`personal_config.json`** (new; the single source of truth for the standalone):
```jsonc
{
  "personal_baseline": 0.0,          // immutable risk anchor (SGD), Telegram-set — NOT live equity
  "active_mode": "conservative",
  "modes": { "conservative": { "risk_pct": 0.01 }, "aggressive": { "risk_pct": 0.02 } },
  "max_lots": 0.0,                    // 0 = no cap (mirror of prop max_prop_lots)
  "daily_dd_pct": 4.0,
  "overall_dd_pct": 8.0,
  "max_open_positions": 2,
  "day_roll": "11:00",               // SGT
  "active": true,                    // master on/off
  "permanently_halted": false,
  "daily_halted": false
}
```
`account currency` is **auto-detected from MT5** (SGD on Fusion). Reuse the existing currency-format
helpers; forex prices carry no symbol. [[sgd-usd-account-currency]]

**Telegram commands (carry over the proven ones, drop prop/phase):**
- Setup/anchor: `/setbaseline <amount>`, `/setdeposit <amount>` (reporting only), `/setrisk <mode> <pct>`.
- Mode: `/mode [conservative|aggressive]`.
- Halts: `/halts`, `/setdailydd <pct>`, `/setoveralldd <pct>`, `/resume`, `/rearm`, `/clearhalt`.
- Ops: `/start` `/stop`, `/status`, `/equity`, `/setwindow`, `/setdayroll`, `/closepair`, `/checksymbols`,
  `/update`.
- **Drop:** `/phase1` `/phase2` `/changepropfirm` and everything prop/consistency/stage-related.

---

## 8. Build phases (for the builder Claude — TDD)

1. **Kernel + geometry** (pure, fully unit-tested first): port `dollar_per_unit` unchanged; implement
   `compute_personal_geometry` per §3; pin numbers with tests mirroring `tests/layer2/test_phase2_strategy.py`
   (the parity example in `02-calculation-parity.md` is the first test case).
2. **Receiver**: FastAPI `/signal`, webhook validation (14-field contract [[webhook-payload-contract]]),
   gate chain (curfew → halted → news/manual-suppress → dedup → max-pos → contract query → geometry →
   preflight → push), Telegram bot, equity monitor + halts + day roll.
3. **Worker**: MT5 self-launch + account guard, PULL execute (retry/limit fallback), REP query,
   close-watcher → journaling.
4. **Integration**: ZMQ wiring, `/update`-style deploy, demo-account soak (≥7 trading days) before live.

---

## 9. Open numbers to confirm (do not block the plan — confirm on review)

- `risk_pct` for each mode (defaults 1% / 2%).
- `daily_dd_pct` / `overall_dd_pct` (suggested 4% / 8%).
- `personal_baseline` value (SGD) and whether `max_lots` should cap.
- Hosting: reuse VPS #2 (Vultr, already runs the personal MT5 terminal) for the Worker; pick a Linux
  host for the Receiver (could be VPS #1 if the 4-layer system is retired, or a fresh droplet).

---

## 10. Hard constraints carried forward
- **Sizing uses the fixed `personal_baseline`, never live MT5 equity.** (Live equity only for halts/reporting.)
- **Direction follows the signal.** No inversion anywhere (the inverse leg is gone).
- **MT5 must be self-launched** by the Worker; hard guard `account_info().login == configured login`
  (fatal exit on mismatch). [[mt5-python-integration-constraints]]
- **Account currency = whatever MT5 reports** (SGD now); never hardcode `$` on the personal side.
- Demo-first: ≥7 trading days before live capital.

---

## 11. Future option Warren is weighing — "which approach is better?" (PARKED, not built)
Warren is considering, later, making personal **follow the prop system's exact entry + calculation but
opposite direction** (prop as the master, personal mirrors) — i.e. a single signal source instead of two.

- **Approach A (current plan): two independent signal sources.** Personal trades its own Layer-0 signal;
  prop trades its own fade signal; personal additionally listens to the prop bot's halts (§5a). Cleaner
  separation, each system testable/deployable alone — matches "independent standalone". *Downside:* the
  two legs are not guaranteed to be exact opposites at the same instant (different signals/timing), so
  it's a loose hedge, not a tight one.
- **Approach B (future): prop master, personal follows inverse.** Personal subscribes to the prop's signal
  feed and trades the exact opposite with mirrored sizing. Guarantees a tight hedge from one signal
  source — essentially what the original 4-layer system does, minus the prohibited concealment. *Downside:*
  re-introduces coupling (personal depends on the prop feed), which is the thing Warren is separating.

**Recommendation:** build **A now** (it's what's specified and keeps the systems clean and independent).
Keep **B as a documented future toggle**: personal could gain a "follow external prop signal" input mode
later without re-architecting, since its geometry kernel is unchanged either way. Decision deferred to
Warren — do not build B unless he asks.
