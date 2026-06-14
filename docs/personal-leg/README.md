# personal-leg/ — Personal System = Inverse Follower of the Prop System (full build kit)

> **What this is:** a complete kit for an autonomous agent to build the **personal trading system** in a
> **new repo**, using `arbitrage-trading` as read-only reference. The personal system has **no signal of
> its own** — it **follows a separate prop system** by reading the prop bot's Telegram alerts and mirroring
> every trade as the **inverse hedge**. This reproduces the original coupled hedge while the prop system
> stays completely unaware of personal. Nothing is built yet.

## ⚠️ The one feasibility constraint (read first)
Telegram **Bot API bots cannot read other bots' messages.** So the prop-alert reader **must** be a
Telegram **user client (MTProto / Telethon)** with a user session (`api_id`+`api_hash`+phone login), not a
bot. This is the blocking item Warren confirms at **CP-0**. Personal's own *control* bot can be a normal bot.

## How to use
Open a session **inside `arbitrage-trading`** and tell the agent: **"Read `docs/personal-leg/03-build-prompt.md`
and follow it."** It reads the kit, creates `~/Coding Projects/personal-leg-system/`, and builds T0→T13,
stopping only at the checkpoints.

## The design in one breath
Personal reads the prop bot's **Trade Opened / Position Closed / Kill** alerts (via MTProto) and, for each,
acts on the personal MT5 account as the exact inverse: `pers_dir = inverse(prop_dir)`,
`pers_lots = round(prop_lots × phase_multiplier, 2)`, `pers_sl = prop_tp`, `pers_tp = prop_sl`. Closes and
kills are mirrored too. Net exposure across both accounts = the original coupled system.

## Files (read in order)
| # | File | Role |
|---|---|---|
| 00 | [`00-AGENT-START-HERE.md`](00-AGENT-START-HERE.md) | **Entry point** — rules, the MTProto feasibility gate, reference map, checkpoints. |
| 01 | [`01-master-plan.md`](01-master-plan.md) | The model (inverse follower), 6 locked decisions, architecture. |
| 02 | [`02-calculation-parity.md`](02-calculation-parity.md) | The reconstruction math (mirror of the prop trade) + first test. |
| 03 | [`03-build-prompt.md`](03-build-prompt.md) | The one-paste kickoff prompt. |
| 04 | [`04-system-architecture.md`](04-system-architecture.md) | Target repo layout, process model, reference→new map. |
| 05 | [`05-data-contracts.md`](05-data-contracts.md) | Prop-alert parse contract, ZMQ ticket, REP queries, `personal_config.json`, env. |
| 06 | [`06-build-tasks.md`](06-build-tasks.md) | **The runbook** — tasks T0→T13 + checkpoints. |
| 07 | [`07-telegram-spec.md`](07-telegram-spec.md) | Control bot commands + the MTProto reader + currency rules. |
| 08 | [`08-test-plan.md`](08-test-plan.md) | Every test case with exact expected values. |
| 09 | [`09-deploy-runbook.md`](09-deploy-runbook.md) | MTProto session setup, MT5 connect, hosting, go-live gates. |
| 10 | [`10-prop-follower.md`](10-prop-follower.md) | **The core** — read + route the prop bot's open/close/kill alerts. |

(`10-prop-halt-listener.md` is a superseded redirect — see `10-prop-follower.md` instead.)

## Decisions locked with Warren (2026-06-14)
1. **Personal follows prop** (Approach B) — no own signal; inverse hedge of every prop trade.
2. **Link = Telegram group**, read via an **MTProto user session** (bot-to-bot is impossible).
3. **Sizing/geometry = the original personal-leg reconstruction** (`prop_lots × phase_mult`, swapped box).
4. **Currency auto-detected** from the personal MT5 (SGD now); never hardcode `$`.
5. The prop system stays pristine — personal reads it; it never references personal.
6. Built by a separate agent from `03-build-prompt.md`.

## Open items Warren confirms
- **CP-0 (blocking):** OK to run an MTProto user session? → `api_id`, `api_hash`, the user account,
  shared `group_chat_id`, `prop_bot_username`.
- **CP-1:** `phase_multipliers` (default 0.20/0.70); the prop alert parse contract (prefer the prop kit's
  `OPEN|CLOSE|KILL` structured line); secondary-DD on/off; control-bot token; personal MT5 login; Firebase
  creds; Receiver host.
