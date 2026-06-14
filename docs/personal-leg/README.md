# personal-leg/ — Standalone Personal-Account Rebuild (master plan)

> **What this is:** the complete plan to rebuild the **personal account leg** as a
> **standalone single-leg trading system** — personal trades alone, no prop/FundingPips hedge.
> Nothing is built yet. These are planning + build-handoff documents only.
>
> **Why it's here and not at repo root:** this repo's filesystem blocks creating new top-level
> directories (EPERM). `docs/personal-leg/` is the single named folder holding the master plan.

## Files

| File | Purpose |
|---|---|
| [`01-master-plan.md`](01-master-plan.md) | The master plan: scope, the 6 resolved design decisions, native sizing/geometry math, two-mode toggle, risk halts, architecture, config + Telegram surface, build phases, open numbers to confirm. |
| [`02-calculation-parity.md`](02-calculation-parity.md) | **The heart of Warren's instruction.** Runs the current 2-leg system end to end (code-accurate), shows exactly how each personal number is derived from prop today, then derives the standalone "prop logic, reversed" native math with worked numbers. Proves the kernel is unchanged. |
| [`03-build-prompt.md`](03-build-prompt.md) | A self-contained prompt to hand a **fresh Claude session** that will actually build the system. Names the exact files to reuse from this repo (no tree-scanning) and the exact files to write. |

## One-line summary of the design

Keep the existing risk kernel **identical** (`lots = risk_$ / (stop_distance × k)`, `k` from
`dollar_per_unit`, direction = follow the signal, SL/TP = raw signal levels). Replace the **prop
dependency** with a **native personal anchor**: `risk_$ = personal_baseline × risk_pct`, sized over
the **personal leg's own stop** (`|entry − signal_sl|`). This is the prop's own sizing method applied
in the reverse (signal-following) direction — the opposite end of the same SL/TP box.

## Decisions locked with Warren (2026-06-14)

1. **Sizing anchor:** % of a **fixed personal baseline** set via Telegram (immutable like today's prop anchor).
2. **Phases:** dropped. **Two-mode toggle** instead — modes differ by **risk % only**, identical geometry.
3. **Geometry:** **raw signal SL + TP**, computed exactly as the prop's logic but in the reverse (personal) direction.
4. **Risk halts:** **daily + overall drawdown halt** on personal equity (mirror of prop K1/K2 logic).
5. **Architecture:** clean **greenfield 2-service** (Linux Receiver + Windows MT5 Worker); reuse Layer 0 Pine, symbol mapper, journaling, MT5 self-launch rule, webhook→ZMQ→MT5 transport.
6. **Build discipline:** plan + build-prompt only here; a separate Claude session builds from `03-build-prompt.md`.
