# Sample System Logs — Trade Execution Engine

This file shows representative log output from each layer of the TEE system.
Use this as a reference when capturing real logs from VPS #1.

---

## How to Capture Real Logs

SSH into VPS #1 and run the following commands to capture logs:

```bash
# Layer 1 — Gatekeeper service log (last 100 lines)
sudo journalctl -u layer1.service -n 100 --no-pager > layer1_log.txt

# Layer 2 — Orchestrator service log (last 100 lines)
sudo journalctl -u layer2.service -n 100 --no-pager > layer2_log.txt

# Service status snapshot
sudo systemctl status layer1.service > layer1_status.txt
sudo systemctl status layer2.service > layer2_status.txt

# Health check
curl -s https://api.warrenlimzf.com/health
```

Save the output files and add them to this folder.

---

## Representative Layer 1 Log Output

```
2026-05-09 08:14:32 [INFO] Layer 1 Gatekeeper starting on 0.0.0.0:8000
2026-05-09 08:14:32 [INFO] Finnhub API key loaded
2026-05-09 08:14:32 [INFO] Allow-list loaded: ['XAUUSD','USDJPY','BTCUSD','ETHUSD','FTSE100','EURUSD']
2026-05-09 08:14:32 [INFO] Ready to receive webhooks at /webhook

2026-05-09 09:32:17 [INFO] Webhook received: symbol=XAUUSD direction=BUY entry=3288.50 sl=3272.00
2026-05-09 09:32:17 [INFO] Symbol XAUUSD in allow-list ✓
2026-05-09 09:32:17 [INFO] Finnhub query: checking events ±30min from 09:32:17 SGT
2026-05-09 09:32:17 [INFO] No high-impact events found in window
2026-05-09 09:32:17 [INFO] Signal PASSED gatekeeper — forwarding to Layer 2

2026-05-09 14:00:05 [INFO] Webhook received: symbol=USDJPY direction=SELL entry=152.810 sl=153.200
2026-05-09 14:00:05 [INFO] Symbol USDJPY in allow-list ✓
2026-05-09 14:00:06 [INFO] Finnhub query: checking events ±30min from 14:00:05 SGT
2026-05-09 14:00:06 [WARNING] HIGH-IMPACT EVENT DETECTED: "US CPI" at 14:30 SGT (in 29.9 min)
2026-05-09 14:00:06 [INFO] Signal SUPPRESSED — news filter active
```

---

## Representative Layer 2 Log Output

```
2026-05-09 08:14:35 [INFO] Layer 2 Orchestrator starting
2026-05-09 08:14:35 [INFO] ZMQ dealer socket connecting to VPS2 tcp://[VPS2_IP]:5555
2026-05-09 08:14:35 [INFO] ZMQ dealer socket connecting to VPS3 tcp://[VPS3_IP]:5556
2026-05-09 08:14:35 [INFO] Telegram bot initialized — polling started (chat_id=6670876447)
2026-05-09 08:14:35 [INFO] Kill monitor thread started (30s interval)
2026-05-09 08:14:35 [INFO] SGT curfew scheduler started

2026-05-09 08:14:50 [INFO] Kill monitor: querying equity from both workers
2026-05-09 08:14:50 [INFO] Prop equity: $10,248.17 | Hedge equity: $4,891.33
2026-05-09 08:14:50 [INFO] Kill 1 check: daily loss = $0.00 (limit: $200.00) ✓
2026-05-09 08:14:50 [INFO] Kill 2 check: overall DD = 0.00% (limit: 5.00%) ✓
2026-05-09 08:14:50 [INFO] Kill 3 check: daily profit = $0.00 (limit: $250.00) ✓
2026-05-09 08:14:50 [INFO] Kill 4 check: overall profit = 2.48% (limit: 10.00%) ✓
2026-05-09 08:14:50 [INFO] All kill conditions clear

2026-05-09 09:32:18 [INFO] Signal received from Layer 1: XAUUSD BUY
2026-05-09 09:32:18 [INFO] Querying contract data for XAUUSD from both workers
2026-05-09 09:32:18 [INFO] Prop: point=0.01 tick_value=0.0988 contract_size=100
2026-05-09 09:32:18 [INFO] Computing prop lot: risk=$67.23 sl_distance=16.50 → lots=0.41
2026-05-09 09:32:18 [INFO] Computing hedge lot: risk=... → lots=0.12
2026-05-09 09:32:19 [INFO] ZMQ ORDER → Layer 3A: BUY XAUUSD 0.41 lots @ market SL=3272.00 TP=3349.63
2026-05-09 09:32:19 [INFO] ZMQ ORDER → Layer 3B: SELL XAUUSD 0.12 lots @ market SL=3305.00 TP=3271.85
2026-05-09 09:32:20 [INFO] Layer 3A confirmation: ticket=12948271 opened
2026-05-09 09:32:20 [INFO] Layer 3B confirmation: ticket=83921047 opened
2026-05-09 09:32:20 [INFO] Telegram: Trade opened — XAUUSD BUY 0.41 lots (prop) / SELL 0.12 lots (hedge)
```

---

## Telegram Bot Commands (Reference)

```
/help      → Lists all commands and kill condition thresholds
/phase1    → Activate Phase 1 (locks baseline equity, 0.67% risk/trade)
/phase2    → Activate Phase 2 (adjusts hedge ratio)
/resume    → Resume signal processing after halt
/forcestop → Manual FORCE_CLOSE on both accounts
/changepropfirm → Wizard to update prop firm configuration
```

---

## Instructions for Real Log Capture

1. SSH into VPS #1: `ssh root@152.42.213.98`
2. Run: `sudo journalctl -u layer2.service -f` — shows live log stream
3. Screenshot the terminal window showing live log lines
4. Also screenshot: `sudo systemctl status layer1.service layer2.service`
5. Screenshot the Telegram chat showing bot responses to /help and /phase1
6. Curl the health endpoint: `curl https://api.warrenlimzf.com/health`
