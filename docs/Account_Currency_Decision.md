# Account Currency Decision — SGD vs USD (Personal / Fusion Markets)

**Status:** DECIDED — open the real Fusion Markets personal account in **USD**, not SGD.
**Decided:** 2026-05-19 (session 14)
**Scope:** Applies the moment real capital is funded. Has no effect on the current USD demo soak.

This file is the canonical answer. If the SGD/USD question is asked again, restate the
conclusion below and point here — do **not** re-derive from the code each time.

---

## The original concern

> "My personal account (Fusion Markets) will be SGD-denominated; the prop account
> (FundingPips) is USD. Fusion converts SGD↔USD for free on entry/exit. Will the
> system's calculations be messed up if personal is SGD and prop is USD? What are
> the significant risks?"

Both demo accounts in use today are `MetaQuotes-Demo` and **USD** — so nothing is
wrong *today*. The question only matters at go-live, when the real Fusion account
is funded.

---

## Findings (traced through the actual code)

Files traced: `layer2/phase1_strategy.py`, `layer2/phase2_strategy.py`,
`layer2/strategy_common.py`, `layer2/logic_core.py`, `layer3/_worker_core.py`,
`layer2/telegram_handlers.py`.

### What is NOT affected (safe)

1. **Lot sizing — safe.** Personal lots are never derived from the personal
   baseline or personal account currency. In both phases:
   `pers_lots = round(prop_lots × phase_ratio, 2)` (0.20 Phase 1 / 0.70 Phase 2).
   `prop_lots` is computed purely from the **prop** baseline (USD) and the **prop**
   MT5 account's tick data (USD) — fully self-consistent. `pers_baseline_equity`
   (the number typed at wizard Step 10/10) feeds **nothing** in the trade math; it
   is a display/reference figure only.

2. **Kill conditions K1–K5 — safe.** Every kill (`phase1_strategy.evaluate_kills`
   and the Phase 2 path in `logic_core`) is computed from prop equity, prop
   baseline, prop day-start — all USD, all from FundingPips. Personal equity is
   read but only stored as `pers_day_start_equity`; it never gates a kill.

3. **No hidden SGD+USD addition in code.** There is no "combined P&L" anywhere;
   the close alert builds two independent side-blocks. SGD and USD only get mixed
   in a human reading two `$`-labelled numbers.

### Where a SGD personal account WOULD bite (display / mental accounting only)

These are why we chose USD — none break execution, but all corrupt judgement:

- **RISK 1 — Hedge-evaluation error (worst).** Every Trade Opened/Closed alert
  prints both legs with `$`. On a SGD personal account, personal figures are SGD
  and prop figures are USD, both labelled `$`. Eyeballing "personal +$50 / prop
  −$48 → net +$2" silently adds SGD to USD; at ~1.30 USDSGD a personal "$50" is
  really ~$38 USD. This corrupts the very judgement the hedge depends on.
- **RISK 2 — Mislabeled risk on USDJPY / USDCHF / USDCAD.** For these 3 pairs the
  quote currency is not USD, so the code falls back to MT5 `trade_tick_value`,
  which the broker returns in the account deposit currency (SGD). Displayed
  personal risk/reward for these 3 pairs is overstated ~30%. (The other 5 pairs —
  EURUSD, GBPUSD, NZDUSD, XAUUSD, XAGUSD — use `contract_size` and stay correctly
  USD.) Sizing is still fine; only the displayed number is wrong-unit.
- **RISK 3 — FX translation drift.** The hedge offsets the two legs' *USD* P&L,
  but the personal leg realizes into SGD at the live rate, so the *home-currency*
  net of a "neutral" hedge is not exactly zero (residual ≈ USDSGD move × leg
  gross P&L). Small, second-order, but path-dependent on close timing.
- **RISK 4 — Split-currency net worth.** Prop baseline USD vs personal baseline
  SGD: aggregate worth moves with USDSGD even with no open trade.
- **RISK 5 — "Free" conversion isn't spread-free.** Fusion's SGD↔USD conversion
  is commission-free but uses their rate (carries a markup). Recurring drag on
  the personal leg vs prop.

---

## Decision & rationale

**Open the real Fusion Markets account in USD.**

- Zero code change to a money-critical system on the eve of go-live.
- Live behaviour becomes *identical* to the USD demo that was already soaked —
  the demo evidence transfers directly.
- All five risks above disappear.
- Only cost: fund/withdraw via SGD↔USD at the bank/broker boundary instead of
  per-trade — economically near-identical, one-time conversion spread per deposit.

Rejected alternative: keep SGD + add a USD-normalization layer (account_currency
field + USDSGD rate, convert all alerts/status/journal to USD). More correct
long-term but is new code in the trade-reporting path requiring its own fresh
demo soak — not worth the go-live risk when a USD account solves it for free.

---

## The actual workflow (go-live, currency-related steps)

1. **When opening the broker account:** select **USD** as the Fusion Markets
   account base currency (Fusion offers USD for forex accounts). FundingPips is
   already USD.
2. **VPS #3 (worker-personal, Fusion):** before switching MT5 to live
   credentials, confirm the account currency reads **USD** in MT5.
3. **VPS #2 (worker-prop, FundingPips):** confirm **USD** likewise.
4. **`/changepropfirm` wizard:** at Step 10/10 enter `pers_baseline_equity` as the
   real **USD** starting balance of the Fusion account (so `/status` and `/equity`
   Personal-block percentages stay correct and comparable to the prop block).
5. Proceed with the normal Go-Live Checklist (TECHNICAL.md). The currency check
   is recorded there as step 0 and as a Hard Constraint in CLAUDE.md.

If a USD Fusion account is somehow impossible: do **not** go live in SGD. The
USD-normalization code layer + a fresh ≥7-day demo soak becomes mandatory first.

---

## One-line answer (for re-asks)

> Personal account currency does not affect lot sizing or kills (personal lots =
> prop_lots × phase_ratio; all kills are prop-side USD). SGD would only mislabel
> personal P&L/risk and make the two hedge legs non-comparable. Decision: open
> Fusion in USD — see `docs/Account_Currency_Decision.md`.
