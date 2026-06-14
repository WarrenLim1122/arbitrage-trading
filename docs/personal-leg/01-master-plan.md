# Master Plan — Personal Leg = Inverse Follower of the Prop System (via Telegram)

Status: **PLAN ONLY — not built.** Build is run by a fresh agent via `03-build-prompt.md`.
Reconstruction math: `02-calculation-parity.md`. Prop-event handling: `10-prop-follower.md`.

> **Design model (locked with Warren 2026-06-14):** the personal leg has **no signal of its own**. It
> **follows the prop system's trades** and acts as the **inverse hedge** — reproducing the original
> coupled system's net behavior, but decoupled so the prop system has **zero knowledge** of personal.
> The link is the **Telegram group**: the prop bot posts its normal trade/close/kill alerts; the personal
> system **reads them** and mirrors each event on the personal account in the opposite direction.

---

## 1. Goal & the two roles
- **Prop system (separate repo, `docs/prop-leg/`):** the **master**. Trades its own breakout-fade signal
  with the full prop-firm-challenge logic. Looks and behaves like a fully independent system. **Never
  references personal.**
- **Personal system (this kit):** the **follower/hedge**. No own Pine, no own webhook signal. It reads the
  prop bot's Telegram alerts and, for each prop event, acts on the personal MT5 account as the **exact
  inverse** — using the original system's personal-leg logic (`pers_lots = prop_lots × phase_multiplier`,
  the mirror box). Net effect = the original coupled hedge.

## 2. ⚠️ Feasibility constraint — read before building (the one hard requirement)
Telegram **Bot API bots cannot read messages from other bots.** So personal's *bot* cannot see the prop
*bot's* alerts in a group. The prop-event reader on the personal side **must be a Telegram user client
(MTProto — Telethon or Pyrogram)** logged in with a **user account**, which can read all group messages
including other bots'.
- **Prop bot:** normal Bot API bot (posts its own alerts). Unchanged, unaware.
- **Personal reader:** MTProto **user session** — needs a Telegram `api_id` + `api_hash`
  (from my.telegram.org) and a one-time phone login. **This is the #1 thing Warren confirms (CP-0).**
- **Personal control bot:** a normal Bot API bot is fine for Warren's own `/status`, `/stop`, etc.
- If Warren does not want a user session, the follow-via-Telegram model is not buildable as-is — surface
  this at CP-0 before writing the reader.

## 3. What personal does on each prop event (the core)
Personal subscribes (MTProto) to the shared group and routes the prop bot's messages:

| Prop event (Telegram alert) | Personal action |
|---|---|
| **Trade Opened** (pair, prop direction, entry, prop SL, prop TP, prop lots, phase) | Open the **inverse** hedge on that pair (see `02`): `pers_dir = inverse(prop_dir)`, `pers_lots = round(prop_lots × phase_mult[phase], 2)`, `pers_sl = prop_tp`, `pers_tp = prop_sl`. |
| **Position Closed** (pair) | Close personal's matching position on that pair (unwind the hedge together). |
| **Kill K1–K5 / force-close / halt** | Close the affected position(s) and halt per policy (`10 §4`). |

Personal matches prop trades to its own positions **by pair** (one open position per pair; dedup). Full
parsing + action spec: `10-prop-follower.md`.

## 4. Sizing & geometry — reconstruction, not native (full detail in `02`)
Personal does **not** size from its own baseline. It uses the **original system's personal-leg logic**,
reconstructed from what the prop publishes:
```
pers_direction = inverse(prop_direction)          # LONG<->SHORT
pers_lots      = round(prop_lots × phase_mult[phase], 2)   # phase_mult {1: 0.20, 2: 0.70}
pers_sl        = prop_tp                            # personal SL = prop TP price
pers_tp        = prop_sl                            # personal TP = prop SL price
```
This is byte-identical to the original `pers_*` relationship (verified against `phase1_strategy.py` and
`phase2_strategy.py`: in both phases `pers_sl = prop_tp`, `pers_tp = prop_sl`,
`pers_lots = prop_lots × phase_ratio`, `pers_dir = invert(prop_dir)`). No personal baseline, no
two-mode toggle, no geometry-from-signal — those belonged to the abandoned independent design.

## 5. Architecture — 2-service, driven by Telegram (greenfield)
```
              Shared Telegram group
   prop bot ─(posts its own trade/close/kill alerts)─┐
                                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ RECEIVER  — Linux VPS, systemd                                                 │
│  • MTProto user-session reader → prop-event router (10)                        │
│  • hedge reconstruction (04/02) → ZMQ PUSH ticket                              │
│  • reconciliation monitor (poll worker; match prop closes; worker health)      │
│  • personal CONTROL bot (Bot API): /status /stop /positions /equity ...        │
└─────────────────── ZMQ PUSH :5555 / REQ :5556 ────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ WORKER  — Windows VPS (personal MT5 terminal)                                  │
│  • MT5 self-launch + hard account guard ; PULL execute (retry/limit fallback)  │
│  • REP queries ; position-close watcher → journaling                           │
└──────────────────────────────────────────────────────────────────────────────┘
```
**No** `/signal` webhook, **no** TradingView Pine, **no** news filter on the personal side — the prop
system already applied news/time/curfew gating before it traded, so if prop didn't trade there's simply
no message and personal does nothing. Personal is a faithful inverse shadow.

### Reuse from the reference (lift, don't rewrite)
`layer2/strategy_common.py` (`invert_signal`); `layer2/symbols.py` + `config/symbols.json`;
`layer3/symbol_mapper.py`; `layer3/journal/`; MT5 self-launch + account guard from `layer3/_worker_core.py`;
the personal-leg `pers_*` relationship from `layer2/phase1_strategy.py` / `phase2_strategy.py`; SGT
day-roll/currency helpers from `layer2/state.py`; Telegram format standards. [[telegram-reporting-standards]]

## 6. Decisions locked with Warren (2026-06-14)
1. **Personal follows prop** (Approach B) — no own signal; inverse hedge of every prop trade.
2. **Link = Telegram group**, read via an **MTProto user session** (bot-to-bot is impossible).
3. **Sizing/geometry = original personal-leg reconstruction** (`prop_lots × phase_mult`, mirror box).
4. **Currency auto-detected from the personal MT5** (SGD now); never hardcode `$`.
5. Prop system stays pristine — personal reads it; it never references personal.
6. Built by a separate agent from `03-build-prompt.md`.

## 7. Config & personal control Telegram surface
`personal_config.json` (schema `05 §4`): MTProto creds + session path, shared `group_chat_id`, prop bot
identity, `phase_multipliers {1:0.20, 2:0.70}`, prop-event keyword/parse map + action policy, max open
positions, day-roll, `active`/halt flags, optional secondary personal DD halt. Control commands (personal
bot): `/status`, `/stop`/`/start`, `/positions`, `/equity`, `/health`, `/follow on|off` (pause following),
`/closepair`, `/checksymbols`, `/update`, `/help`. Full list: `07-telegram-spec.md`.

## 8. Open items to confirm (CP-0/CP-1)
- **CP-0 (blocking):** Warren OK to run an MTProto **user session** for the reader? Provide `api_id`,
  `api_hash`, the user account/phone for login, and the shared `group_chat_id` + prop bot username/id.
- **CP-1:** `phase_multipliers` (default 0.20/0.70 — confirm); the prop alert **parse contract** (confirm
  the prop's trade/close/kill alert format, or have the prop kit emit the structured line in `10 §3`);
  whether personal keeps a secondary own DD halt; Receiver host; personal MT5 login; Firebase creds.

## 9. Hard constraints
- **Personal sizing/geometry is reconstructed from the prop's published trade** — never from a personal
  baseline or live equity. `pers_lots = prop_lots × phase_mult`; `pers_sl=prop_tp`; `pers_tp=prop_sl`;
  `pers_dir = inverse(prop_dir)`.
- **The prop system is never modified and never references personal.** All coupling lives here.
- **MT5 self-launched** by the Worker; hard guard `account_info().login == configured login` (fatal exit
  on mismatch). [[mt5-python-integration-constraints]]
- **Account currency = whatever the personal MT5 reports**; never hardcode `$`.
- Demo-first: ≥7 trading days before live capital.
