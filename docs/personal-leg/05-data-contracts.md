# 05 — Data Contracts (exact)

Personal's **input** is the prop bot's Telegram alerts (read via MTProto); its **output** is a ZMQ ticket
to the personal Worker. No webhook.

## 1. Prop-event parse contract (the prop bot's alerts → event dict)
Personal parses three prop alert types. **Recommended:** have the prop kit emit a stable **structured
line** in each alert (see `prop-leg/07` — framed there generically as an audit/integration line, with no
mention of personal), and parse that line. Fall back to keyword parsing of the human text if absent.

**Structured line format (what personal expects to find in the prop alert):**
```
OPEN|pair=EURUSD|dir=SHORT|entry=1.08500|sl=1.08554|tp=1.08300|lots=18.52|phase=2
CLOSE|pair=EURUSD|reason=TP
KILL|k=K1|scope=account            # or scope=EURUSD for a pair-specific force-close
```
Parsed event dicts:
```python
{"type":"open","pair":str,"dir":"LONG|SHORT","entry":float,"sl":float,"tp":float,"lots":float,"phase":int}
{"type":"close","pair":str,"reason":str}
{"type":"kill","k":"K1..K5|FORCE","scope":"account"|"<pair>"}
```
**Sender filter:** only parse messages whose sender is the configured prop bot (`prop_bot_username`/id).
Ignore everything else (including personal's own control-bot messages).

## 2. ZMQ execution ticket — Receiver PUSH :5555 → Worker PULL
```python
ticket = {
    "signal_id":    f"{base_id}",        # unique id (timestamp+pair)
    "ticker":       event["pair"],
    "timestamp_ms": <now_ms>,
    "entry":        <0 or prop entry; market order ignores it>,
    "sl":           recon["sl"],          # = prop_tp
    "tp":           recon["tp"],          # = prop_sl
    "sl_pips":      0,                    # not needed; journal-only in the original
    "signal":       recon["signal"],      # = invert(prop dir)
    "lots":         recon["lots"],        # = round(prop_lots × phase_mult, 2)
    "order_type":   "market",
}
```
For a **close** event personal sends a close instruction for the matching pair (a `FORCE_CLOSE` ticket or
a REP `close` query — mirror the reference worker's force-close path), keyed by pair.

## 3. ZMQ REP queries — Receiver REQ :5556 → Worker (timeout 3s)
Same protocol as the reference (`_worker_core.py:1480`): `equity` (contract info, `account_currency`,
`usd_to_acct_rate`, `trade_allowed`, +fee iff `want_fee`), `positions`, `order_status`, `order_check`,
`deal_pnl` (strict `position_id`+`DEAL_ENTRY_OUT`, window `now+1day` — MT5 deal.time is server-tz),
`account_mode`, `checksymbols`, `reset_fee_anchor`. Personal needs at least `equity`, `positions`,
`order_check`, `order_status`, `deal_pnl`.

## 4. `config/personal_config.json`
```jsonc
{
  // --- MTProto reader (the prop link) ---
  "mtproto": {
    "api_id": 0,                       // from my.telegram.org   (CP-0)
    "api_hash": "",                    // from my.telegram.org   (CP-0)
    "session_path": "secrets/personal_reader.session",
    "group_chat_id": null,             // shared group both systems sit in   (CP-0)
    "prop_bot_username": null          // only act on this sender             (CP-0)
  },
  // --- following ---
  "follow_enabled": true,              // master follow on/off (/follow)
  "phase_multipliers": { "1": 0.20, "2": 0.70 },   // CONFIRM (matches the original)
  "max_open_positions": 2,             // personal positions
  "parse": {                           // keyword fallback if the structured line is absent
    "open_keywords": ["Trade Opened", "OPEN"],
    "close_keywords": ["Position Closed", "CLOSE"],
    "kill_keywords": { "K1":["KILL 1","Daily Loss"], "K2":["KILL 2","Overall"],
                       "K3":["KILL 3","Daily Profit Cap"], "K4":["KILL 4","Profit Target"],
                       "K5":["KILL 5","Consistency"], "FORCE":["FORCE_CLOSE","HALT"] }
  },
  "kill_action": {                     // CONFIRM at CP-1
    "pair_scope": "close_pair",
    "account_permanent": "close_all_and_halt",   // K2/K4/K5
    "account_daily": "close_all"                 // K1/K3
  },
  // --- ops ---
  "day_roll": "11:00",                 // SGT
  "active": true,                      // master on/off (/start /stop)
  "permanently_halted": false,
  "daily_halted": false,
  // --- optional secondary protection (off by default; personal mainly follows prop) ---
  "secondary_dd": { "enabled": false, "daily_pct": 0.0, "overall_pct": 0.0, "baseline": 0.0 },
  "deposit": 0.0                       // reporting/% only
}
```
`account_currency` is read live from MT5 (not stored). **Personal never sizes from a baseline** — sizing
comes from `prop_lots × phase_mult` (`02`). `secondary_dd` is an optional own-equity safety net, off by
default since personal mirrors the prop's halts.

## 5. Env (`secrets/.env`)
**Receiver:** `TELEGRAM_BOT_TOKEN` (control bot), `TELEGRAM_CHAT_ID`, `ZMQ_PUSH_CONNECT`,
`ZMQ_REQ_CONNECT`. (MTProto `api_id`/`api_hash`/session live in config/secrets.)
**Worker:** `MT5_LOGIN` (hard guard), `MT5_TERMINAL_PATH` (optional), `ZMQ_PULL_BIND`, `ZMQ_REP_BIND`,
`FIREBASE_JOURNAL_ENABLED=true`, `FIREBASE_JOURNAL_DRY_RUN=false`,
`GOOGLE_APPLICATION_CREDENTIALS=secrets/firebase-service-account.json`.
> Journaling silent-death guard: WARN on worker startup if journaling env is off. Re-set after any rebuild.
