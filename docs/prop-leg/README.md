# prop-leg/ — Standalone Prop-Firm-Challenge System (full build kit)

> **What this is:** a complete, self-contained kit for an autonomous agent to build a **standalone
> prop-firm-challenge trading system** in a **new repo**, using `arbitrage-trading` as read-only
> reference. The system trades its **own** breakout-fade signal, runs a two-phase challenge (stage ladder
> → funded), and enforces five hard kills (K1–K5). Nothing is built yet.

## 🔒 Naming rule (applies to every built artifact)
This system trades **on its own**. In all code/Pine/config/logs/Telegram, **never** reference another
account or system and **never** use *personal / inverse / mirror / hedge / flip / opposite*. Describe
everything in this system's own absolute terms. Full statement in `00-AGENT-START-HERE.md`.

## How to use
Open a session **inside `arbitrage-trading`** and tell the agent: **"Read `docs/prop-leg/03-build-prompt.md`
and follow it."** The agent reads the kit, creates `~/Coding Projects/prop-leg-system/`, and builds
T0→T14, stopping only at the checkpoints.

## Files (read in order)
| # | File | Role |
|---|---|---|
| 00 | [`00-AGENT-START-HERE.md`](00-AGENT-START-HERE.md) | **Entry point** — rules, naming rule, reference map, checkpoint protocol. |
| 01 | [`01-master-plan.md`](01-master-plan.md) | Decisions, the two-phase challenge, kills, sizing anchor, architecture. |
| 02 | [`02-calculation-spec.md`](02-calculation-spec.md) | Exact geometry (P1 stage-ladder + P2 box) + kills K1–K5 + buffers, worked numbers. |
| 03 | [`03-build-prompt.md`](03-build-prompt.md) | The one-paste kickoff prompt. |
| 04 | [`04-system-architecture.md`](04-system-architecture.md) | Target repo layout, process model, reference→new map. |
| 05 | [`05-data-contracts.md`](05-data-contracts.md) | Webhook, ZMQ ticket, REP queries, `account_config.json`, env. |
| 06 | [`06-build-tasks.md`](06-build-tasks.md) | **The runbook** — tasks T0→T14 + checkpoints. |
| 07 | [`07-telegram-spec.md`](07-telegram-spec.md) | Commands (challenge wizards) + message formats + currency. |
| 08 | [`08-test-plan.md`](08-test-plan.md) | Every test case + the kills-fire simulation. |
| 09 | [`09-deploy-runbook.md`](09-deploy-runbook.md) | Demo deploy, MT5 connect, hosting, go-live gates. |
| 10 | [`10-signal-engine-pine.md`](10-signal-engine-pine.md) | The system's own Pine indicators + webhook contract. |

## Decisions locked with Warren (2026-06-14)
1. **Full prop-firm challenge logic** — phases, stage ladder, K1–K5, consistency, profit target, baseline
   anchor, buffers — ported, single-account.
2. **Signal:** tight-stop / far-target breakout-fade (RR ≈ 3.7), described as the system's own strategy.
3. **Currency auto-detected from MT5** (no hardcoded symbol).
4. **Independent standalone system** — no coupling to, and no mention of, any other system.
5. **Own Pine indicators** (breakout-fade / RSI-div-fade / NW-fade), per `10`.
6. Built by a separate agent from `03-build-prompt.md`.

## Open numbers Warren confirms at CP-1 (non-blocking for the code build)
`baseline_equity`, `profit_target_pct`, overall/daily DD %, `min_profit_days`, the `/phase1` reward:risk
pair, `consistency_threshold_pct`, `risk_pct` (+ keep the mode toggle?), `propfirm_day_roll` (match the
firm dashboard) · Telegram token, Firebase creds, MT5 login, Receiver host.
