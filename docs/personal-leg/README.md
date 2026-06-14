# personal-leg/ — Standalone Personal-Account Rebuild (full build kit)

> **What this is:** a complete, self-contained kit for an autonomous agent to **rebuild the personal
> account leg as a standalone single-leg trading system** (personal trades alone, no prop hedge) in a
> **new repo**, using the `arbitrage-trading` repo as reference. Plan + exact contracts + a rigid
> ordered task runbook + tests + deploy. Nothing is built yet.
>
> **Why here, not repo root:** this repo's filesystem blocks new top-level dirs (EPERM). The *new build*
> happens in a separate repo (greenfield, no such limit).

## How to use this kit
Hand an agent the kickoff in **`03-build-prompt.md`**. It tells the agent to open **`00-AGENT-START-HERE.md`**
and run the task list in **`06-build-tasks.md`** from T0 to T14, stopping only at the labelled checkpoints.

## Files (read in this order)

| # | File | Role |
|---|---|---|
| 00 | [`00-AGENT-START-HERE.md`](00-AGENT-START-HERE.md) | **Entry point.** Operating rules, the read-order, reference-file map, the checkpoint protocol. |
| 01 | [`01-master-plan.md`](01-master-plan.md) | The "why": 6 locked decisions, native math, two-mode toggle, halts, architecture, open numbers. |
| 02 | [`02-calculation-parity.md`](02-calculation-parity.md) | The math proof — current 2-leg trace → "prop logic, reversed" native formula, worked numbers. |
| 03 | [`03-build-prompt.md`](03-build-prompt.md) | The one-paste kickoff prompt for a fresh Claude session. |
| 04 | [`04-system-architecture.md`](04-system-architecture.md) | Exact target repo layout, process model, every file to create, old→new mapping. |
| 05 | [`05-data-contracts.md`](05-data-contracts.md) | Exact schemas: 14-field webhook, ZMQ ticket, REP queries, `personal_config.json`, env. |
| 06 | [`06-build-tasks.md`](06-build-tasks.md) | **The runbook** — numbered tasks T0→T14, each with reference/spec/tests/acceptance/commit + checkpoints. |
| 07 | [`07-telegram-spec.md`](07-telegram-spec.md) | Every command + message format + currency rules. |
| 08 | [`08-test-plan.md`](08-test-plan.md) | Every test case with exact expected values (TDD). |
| 09 | [`09-deploy-runbook.md`](09-deploy-runbook.md) | Demo deploy, MT5 connect, hosting, go-live gates. |

## The design in one breath
Keep the risk kernel **identical** (`lots = risk_$ / (stop × k)`, `dollar_per_unit` unchanged, follow the
signal, raw signal SL/TP). Replace the prop dependency with a native anchor: `risk_$ = personal_baseline
× risk_pct`, sized over the personal leg's **own** stop (`|entry − signal_sl|`) — the prop's own method
applied in the reverse (personal) direction. Result: a **constant** per-trade risk, no prop needed.

## Decisions locked with Warren (2026-06-14)
1. Sizing = % of a **fixed personal baseline** (Telegram-set, immutable).
2. Phases dropped → **two-mode toggle** differing by **risk % only**, identical geometry.
3. Geometry = **raw signal SL/TP** = the prop's calc logic in the reverse (personal) direction.
4. **Daily + overall DD halt** on personal equity (mirror K1/K2).
5. Clean **greenfield 2-service** (Linux Receiver + Windows Worker); reuse Pine, symbol mapper, journaling, MT5 self-launch, transport.
6. Build is run by a **separate** Claude session from `03-build-prompt.md`; plan-only here.

## Open numbers Warren still confirms (at CP-1; non-blocking for the code build)
Mode `risk_pct` values (default 1% / 2%) · `daily_dd_pct` / `overall_dd_pct` (suggested 4% / 8%) ·
`personal_baseline` (SGD) · Receiver host · Telegram token + Firebase creds + MT5 access.
