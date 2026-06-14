# 10 — Prop-Halt Listener (NEW feature — personal leg only)

> **Status:** new design, 2026-06-14. This feature lives **only** in the personal system. The other
> (prop) system knows nothing about it and is never modified — the coupling is **one-way and loose**
> (personal eavesdrops on messages the prop bot already posts publicly).

## 1. Why
The personal and prop systems are now **separate deployments** on separate accounts. Personal has its own
daily + overall DD halts (T4), but it **cannot see** the prop system's internal K1–K5 kill state. Warren
wants: if the prop system halts a position (a K1–K5 kill / force-close), the personal system should
**also stop the corresponding position**; otherwise personal keeps trading normally.

The clean way to share that signal without coupling code: **both bots sit in one Telegram group.** The
prop bot already posts its kill/halt alerts there. The personal Receiver **listens to that group** and
reacts — exactly the way the existing system's Layer 2 watches the news feed and pre-closes trades. The
prop side is untouched and unaware.

## 2. Design (mirrors the news-filter pattern)
A background listener in the personal Receiver subscribes to the shared Telegram group. For every incoming
message **from the configured prop bot**, it runs a matcher; on a kill/halt match it triggers a
**close/halt action** on the personal side. This is a gate on an external event, just like
`news_filter` — no polling of the prop account, no shared database, no API between the two systems.

```
Shared Telegram group
  ├─ prop bot   → posts its normal kill/halt alerts (K1..K5, force-close, stage events)
  └─ personal bot
        └─ receiver/prop_halt_listener.py   (background, reads group messages)
              match a prop kill/halt alert  →  receiver/monitor closes the matching personal position(s)
              no match                       →  ignore; personal keeps trading
```

## 3. Message-matching contract (parse the prop bot's standard alerts)
The prop bot's kill alerts have a **stable format** (the `msg_kill*` builders). The listener identifies a
halt by **sender identity + keywords**, and extracts the pair if the alert names one:

- **Sender filter:** only act on messages from the configured prop bot (`PROP_BOT_USERNAME` / bot id).
  Ignore everything else in the group (including the personal bot's own messages).
- **Kill keywords** (case-insensitive, any match): `KILL 1`/`K1`/`Daily Loss`, `KILL 2`/`K2`/`Overall
  Drawdown`, `KILL 3`/`K3`/`Daily Profit Cap`, `KILL 4`/`K4`/`Profit Target`, `KILL 5`/`K5`/`Consistency`,
  plus a generic `FORCE_CLOSE` / `HALT`. Maintain the keyword→kill map in config so it survives wording
  tweaks.
- **Pair extraction:** scan the message for any canonical ticker from `config/symbols.json`. If found →
  the action targets that pair. If none found → treat as account-wide.

> Robustness note: parsing another bot's text is inherently best-effort. Keep the keyword map in config,
> log every matched and unmatched prop-bot message at INFO, and alert Warren on a *matched-but-unparseable*
> message so a format drift is caught. **Never** ask the prop side to add markers — it must stay unaware.

## 4. Action policy (config-driven; defaults below — CONFIRM at CP-1)
| Prop alert | Default personal action |
|---|---|
| Names a specific pair (force-close / kill on that pair) | **Close the personal position on that pair** (if open); keep trading other pairs. |
| Account-wide permanent kill (K2 overall / K4 target / K5 consistency) | **Close all personal positions + set a day-halt** (configurable: halt vs close-only). Rationale: the prop account has stopped for the session/permanently; Warren doesn't want personal running on alone. |
| Account-wide day-halt (K1 daily / K3 cap) | **Close all personal positions** (configurable: close-all vs ignore). Default close-all. |

Config (added to `personal_config.json`, see `05`): `prop_halt_listener.enabled` (default true),
`prop_halt_listener.group_chat_id`, `prop_halt_listener.prop_bot_username`,
`prop_halt_listener.keyword_map`, and `prop_halt_listener.action` (per-row policy with the defaults above).
When disabled, personal behaves exactly as if this feature didn't exist.

## 5. Telegram / ops
- `/prophalt` — show listener status (enabled, group, last prop message seen, last action taken).
- `/prophalt on` / `/prophalt off` — toggle at runtime.
- The personal bot posts its own alert when it acts: `msg_prop_halt_action(pair_or_account, kill, action)`
  — so Warren sees *why* personal closed (e.g. "Closed EURUSD — prop reported K1 daily-loss halt").

## 6. Build task (insert into `06-build-tasks.md` as **T8.5**, after the gate chain, before the monitor)

### ▣ T8.5 — Prop-halt listener (TDD)
- **Goal:** personal reacts to the prop bot's kill/halt alerts posted in the shared Telegram group.
- **Spec:** `receiver/prop_halt_listener.py` — a handler on the personal bot's group-message stream that
  (a) filters to the configured prop bot sender, (b) matches the kill keywords, (c) extracts a pair if
  present, (d) calls `monitor.close_positions(pair|all)` + sets the halt per the action policy (§4), and
  (e) sends `msg_prop_halt_action`. Wire it into `telegram_bot.py` (the personal bot must be **in the
  group** and receive group messages — set privacy mode off via BotFather so it sees all group messages).
- **Tests (`tests/test_prop_halt_listener.py`):** a sample prop K1 message naming a pair → closes that
  pair only; a K2 message → closes all + halts; a message from a non-prop sender → ignored; a prop message
  with no kill keyword → ignored; a matched-but-no-pair kill → account-wide action; listener disabled →
  no action. Mock the close/halt calls.
- **Acceptance:** listener tests green.
- **Commit:** `feat: prop-halt listener — close/halt on prop bot's K1–K5 group alerts (personal only)`

## 7. Hard boundary
- This feature touches **only** the personal system. Do **not** add anything to the prop system, and do
  **not** mention the personal system anywhere in the prop build. The prop bot just posts its normal
  alerts; personal listens.
- It does **not** replace personal's own DD halts (T4) — it's an **additional** safety input, like news.
