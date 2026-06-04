# /docs — Reference Documentation

This folder contains documentation about the Trade Execution Engine (TEE) system. It is informational only and does not affect any running code.

**Start at [reference/index.md](./reference/index.md)** — the knowledge base ("brain"). It is the
authoritative, code-verified reference layer (architecture, calculations, messages, execution,
deployment). Consult it before editing code; keep it in sync when code changes.

| File | Purpose |
|------|---------|
| [reference/](./reference/index.md) | **Knowledge base** — architecture, risk/lot/kill calculations, Telegram messages, Layer 3 execution, deployment |
| [System_Architecture.md](./System_Architecture.md) | Full 4-layer agent architecture, signal flow, kill condition state machine, and infrastructure stack |
| [Project_Overview.md](./Project_Overview.md) | End-to-end project description — pain points solved, core workflow, technical stack, measurable outcomes |
| [Sample_Logs.md](./Sample_Logs.md) | Representative Layer 1 and Layer 2 log output, how to capture real logs from VPS #1 |
| [MT5_VPS_Connection_Postmortem.md](./MT5_VPS_Connection_Postmortem.md) | The multi-week MT5 connection debugging journey + diagnostic checklist |
| [Account_Currency_Decision.md](./Account_Currency_Decision.md) | Historical (2026-05-19) currency decision — superseded by the 2026-05-23 SGD reversal |
| [SESSION_HANDOFF.md](./SESSION_HANDOFF.md) | In-flight delta for the next session |
