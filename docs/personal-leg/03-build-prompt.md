# Build Prompt — hand this to a fresh Claude session

Copy everything in the box below into a **new Claude Code session** opened in this repo. It is
self-contained and names exact files to read (no tree-scanning). It builds the standalone personal leg
designed in `01-master-plan.md` / `02-calculation-parity.md`.

> Do **not** start the build from the planning session. Build is a separate session by Warren's choice.

---

```
You are building a STANDALONE single-leg personal trading system, greenfield, in this repo.
It replaces a 4-layer cross-hedge engine: the personal account now trades the signal ALONE, with NO
prop-firm hedge. Use TDD (superpowers:test-driven-development). Do not touch the live 4-layer code paths.

READ THESE FILES FIRST — only these, do not scan the tree:
  Plan (authoritative):
    docs/personal-leg/01-master-plan.md
    docs/personal-leg/02-calculation-parity.md   <- the exact math + first test case
  Kernel to reuse verbatim:
    layer2/strategy_common.py        (dollar_per_unit, invert_signal)
  Reference for the current behavior you are mirroring:
    layer2/phase2_strategy.py        (compute_geometry — the math you reverse onto personal)
    tests/layer2/test_phase2_strategy.py   (test style to copy)
  Components to lift (read when you reach that build phase, not before):
    layer1/news_filter.py, layer1/ff_calendar.py     (news suppression)
    layer2/symbols.py, config/symbols.json           (pair registry)
    layer3/symbol_mapper.py                           (per-broker symbol map + cache)
    layer3/_worker_core.py                            (MT5 self-launch + account guard + execute + REP)
    layer3/journal/                                   (journaling pipeline)
    layer2/state.py  (only: _propfirm_day, propfirm_day_roll, currency-format helpers)
  Constraints/gotchas (skim): docs/reference/architecture.md, docs/reference/execution.md, CLAUDE.md

HARD RULES:
  - This repo's filesystem BLOCKS new top-level dirs/files (EPERM). Put new code inside an EXISTING
    package dir (e.g. layer2/ or a new module under an existing package), NOT a new repo-root folder.
    Confirm placement with Warren before creating files if unsure.
  - NEVER delete/move/overwrite Warren's files. Build alongside the existing system.
  - Auto-commit + push to main after each working unit (Warren's standing rule). Do not deploy to live
    until he says so; demo soak ≥7 trading days first.

WHAT TO BUILD (phases — see 01-master-plan.md §8):
  1. PURE KERNEL + GEOMETRY, tests first:
     - Reuse dollar_per_unit unchanged.
     - compute_personal_geometry(signal, entry, signal_sl, signal_tp, price_digits,
       contract_size, tick_size, tick_value, personal_baseline, risk_pct, max_lots) -> dict|{"reject"}
       per 02-calculation-parity.md §2. risk_$ = personal_baseline*risk_pct; size over
       sl_distance=|entry-signal_sl|; direction FOLLOWS the signal; sl=signal_sl, tp=signal_tp.
     - First test = the worked example in 02-calculation-parity.md §3 (lots==5.00, sl==1.08300,
       tp==1.08554, direction LONG, dollar_risk==1000.0) + a SHORT case + zero-distance reject.
  2. RECEIVER service (Linux, FastAPI, systemd): /signal endpoint, 14-field webhook validation,
     gate chain (SGT curfew -> permanently_halted -> not active -> news/manual suppress -> per-pair
     dedup -> max_open_positions[count PERSONAL positions] -> MT5 contract query over ZMQ -> geometry
     -> single-leg order_check preflight -> PUSH ticket). Telegram bot (all commands + alert text in a
     handlers module; keep orchestration pure). Equity monitor: poll worker equity, daily+overall DD
     halts (01-master-plan.md §5), SGT day roll + auto-resume.
  3. WORKER service (Windows): MT5 self-launch + HARD guard account_info().login == configured login
     (fatal exit on mismatch), PULL execute (market w/ retry + limit fallback), REP query
     (equity/contract/positions/order_check/deal_pnl/checksymbols), position-close watcher -> journaling.
  4. INTEGRATION: ZMQ :5555 PUSH/PULL + :5556 REQ/REP, personal_config.json (schema in 01 §7), a
     /update-style deploy path, demo soak.

CONFIG: personal_config.json per 01-master-plan.md §7. Account currency auto-detected from MT5 (SGD).
  Sizing uses the FIXED personal_baseline, never live equity. Two-mode toggle = risk_pct only.

DROP ENTIRELY: prop worker, inverse-direction leg, phases 1/2, stage ladder, consistency log,
  K3/K4/K5, dual-leg preflight, baseline-as-prop-anchor, /phase*, /changepropfirm.

Before writing the Receiver/Worker, confirm with Warren: the two mode percentages, daily/overall DD %,
the personal_baseline value, and where the Receiver is hosted (01-master-plan.md §9).
```

---

## Notes for whoever runs the build prompt
- The plan keeps the math identical and only removes the prop dependency — resist "improving" the kernel;
  the parity test pins it.
- If the Receiver lands on the same VPS that currently runs Layers 1+2, make sure ports/Telegram token
  don't collide with the still-running 4-layer system until it's formally retired.
