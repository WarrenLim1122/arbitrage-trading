# 10 — Prop Follower (the personal system's entire input)

Personal has no signal of its own. Its **only** input is the prop bot's Telegram alerts, read via an
MTProto **user session** and routed into actions on the personal MT5 account. This file is the full
behavior spec for `receiver/prop_reader.py`, `parser.py`, and `prop_follower.py`.

## 1. The link (and its hard constraint)
- The prop bot posts its normal **Trade Opened / Position Closed / Kill (K1–K5)** alerts to a shared
  Telegram group. The prop system is **unaware** of personal and is never modified.
- **Telegram Bot API bots cannot read other bots' messages.** So the reader **must** be a Telegram
  **user client** (Telethon/Pyrogram) logged in with a user account — it can read all group messages.
  This needs `api_id`+`api_hash` (my.telegram.org) and a one-time phone login (`scripts/mtproto_login.py`).
  **Confirm at CP-0.** Personal's own *control* bot can remain a Bot API bot.

## 2. Reader → parser → follower
```
prop_reader.py (Telethon user client, events.NewMessage in group_chat_id)
   └─ for each message: parse_prop_message(text, sender, cfg)   # parser.py, pure
        └─ event {type: open|close|kill, ...}  →  prop_follower.handle(event)
```

## 3. Parse contract (prefer the structured line; keyword fallback)
The robust path: the prop kit emits a stable **structured line** in each alert (see `prop-leg/07`, framed
there generically as an audit line — no mention of personal). Personal parses it:
```
OPEN|pair=EURUSD|dir=SHORT|entry=1.08500|sl=1.08554|tp=1.08300|lots=18.52|phase=2
CLOSE|pair=EURUSD|reason=TP
KILL|k=K1|scope=account              # scope=<pair> for a pair-specific force-close
```
→ event dicts:
```python
{"type":"open","pair","dir","entry","sl","tp","lots","phase"}
{"type":"close","pair","reason"}
{"type":"kill","k","scope"}
```
Rules: **sender filter** (only the configured `prop_bot_username`/id); extract `pair` against the
canonical registry; if no structured line, fall back to `parse.*_keywords` on the human text; log every
matched + unmatched prop-bot message; alert Warren on **matched-but-unparseable** (catches format drift).
Never ask the prop side to add personal-specific markers — it must stay unaware.

## 4. Actions
### open
1. Guards (drop silently / log if any fail): `follow_enabled`, `active`, not `permanently_halted`, not
   `daily_halted`, pair not already open (dedup), under `max_open_positions`.
2. `query_equity(pair)` → `price_digits` (+ contract data / `trade_allowed`; block if not allowed).
3. `reconstruct_personal(...)` (`02`) → if `{"reject"}`, log + alert, stop.
4. `order_check` pre-flight; reject → `msg`-alert, place nothing.
5. `push_ticket(...)`; record the pair in the by-pair position map; spawn a fill check → `msg_hedge_opened`.

### close
Match by `pair`; if personal has an open position there → `push_close(pair)`; clear the map entry. (The
monitor also reconciles in case a close alert is missed.)

### kill (K1–K5 / FORCE)
Apply `kill_action` (config, `05 §4`):
| Prop kill | Default action |
|---|---|
| `scope=<pair>` (pair force-close) | close that pair (`pair_scope: close_pair`). |
| account-wide **permanent** (K2/K4/K5) | close **all** personal positions + set `daily_halted`/halt (`account_permanent: close_all_and_halt`). |
| account-wide **daily** (K1/K3) | close all personal positions (`account_daily: close_all`). |
Then send `msg_prop_kill_action(scope, k, action)` so Warren sees *why* personal acted.

## 5. Why this reproduces the original hedge
The prop trades its fade signal; personal opens the **inverse** with `prop_lots × phase_mult` and the
swapped box (`02`). When prop closes or is killed, personal unwinds. Net exposure across both accounts =
the original coupled system — but the prop is a clean standalone, and all coupling lives here. Timing: a
small lag (personal acts on the alert, just after the prop fill) is accepted for a hedge.

## 6. Boundaries
- Personal **may** reference the prop openly (it is the follower). The **prop** must never reference
  personal — that rule lives in `prop-leg/00`.
- This follower is personal's **primary** driver. The optional `secondary_dd` own-equity halt (`05 §4`,
  off by default) is independent extra protection, not the main mechanism.
- Build task: **T7** in `06-build-tasks.md`.
