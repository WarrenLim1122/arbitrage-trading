# Session handoff — Phase-2 risk → 1%, and two-repo split build kits (prop master + personal follower)

> Persistent resume file. Paste into a fresh session (or auto-load via a SessionStart hook).
> Delta only — project overview, roles, and decisions live in CLAUDE.md & docs (auto-loaded).
> Full shipped detail / per-session changelog: `docs/SESSION_LOG.md`.

**Role:** Single-agent planning session. No live-system code was built this session beyond the Phase-2
risk tweak; the rest is **planning kits** for a future build agent.

## Status — updated 2026-06-14
- **SHIPPED (live 4-layer system): Phase-2 `prop_risk_pct` 0.67% → 1.0%** (`config/risk_params.json`).
  Phase-2-only; lots already dynamic so they scale up automatically; RR unchanged; Phase 1 (`fixed_risk`)
  and kills untouched. Synced TECHNICAL.md + docs/reference + System_Architecture + the phase2 test
  (114/114 green) + CLAUDE.md hard-constraint line. **DEPLOY: not yet run** — needs `/update layer2` AND a
  real Layer-2 process restart (the constant only reloads on restart; `git pull` alone won't).
- **WROTE two full autonomous build kits (plan-only, nothing built):** Warren is splitting the system into
  two standalone repos that together reproduce the original hedge:
  - `docs/prop-leg/` (12 files) — standalone **MASTER**: full prop-firm challenge logic (phases/stage-
    ladder/K1–K5/consistency/buffers) ported single-account, own breakout-fade Pine (tight-stop/far-target
    RR≈3.7), currency auto-detect. **Hard naming rule: no personal/inverse/mirror/hedge/flip anywhere in
    built artifacts.** Emits an `OPEN|CLOSE|KILL` structured audit line in its alerts.
  - `docs/personal-leg/` (11 files + a superseded stub) — **FOLLOWER**: no own signal; reads the prop
    bot's Telegram alerts and acts as the exact inverse hedge (`pers_dir=invert(prop)`,
    `pers_lots=prop_lots×phase_mult {1:0.20,2:0.70}`, `pers_sl=prop_tp`, `pers_tp=prop_sl`).
- **Design note:** personal was first drafted as Approach A (own signal + halt-listener) then **rewritten
  to Approach B** (follows prop) when Warren clarified intent. `10-prop-halt-listener.md` is now a redirect
  stub → `10-prop-follower.md`.

## Next actions
1. **Deploy the 1% risk change** when Warren is ready: `/update layer2`, then confirm the Layer-2 process
   actually restarted (not just pulled).
2. **To build either kit:** open this repo, tell a fresh agent *"read `docs/<prop-leg|personal-leg>/03-build-prompt.md`
   and follow it."* It builds in a sibling repo (`~/Coding Projects/prop-leg-system` / `personal-leg-system`),
   T0→T13/14, stopping at checkpoints. **Build prop FIRST** (personal needs prop's alerts to follow).
3. Do NOT build from a planning session; do NOT touch the frozen 4-layer code.

## Running state
- Background processes: none
- Dev servers / ports: none
- Worktrees / branches: none (on `main`, all work committed + pushed)

## Open items (await Warren — non-blocking for the code build)
- **Personal CP-0 (BLOCKING feasibility gate):** Telegram Bot API bots can't read other bots' messages →
  the prop-alert reader MUST be an **MTProto user client (Telethon)** with a user session
  (`api_id`+`api_hash`+phone login). Warren must approve this and supply creds + shared `group_chat_id` +
  `prop_bot_username`. If he won't run a user session, the follow-via-Telegram model isn't buildable as-is.
- **Personal CP-1:** `phase_multipliers` (default 0.20/0.70), the prop alert parse contract, secondary-DD
  on/off, control-bot token, personal MT5 login, Firebase creds, Receiver host.
- **Prop CP-1:** baseline, target/overall-DD/daily-DD/consistency %, min-profit-days, the reward:risk pair,
  `propfirm_day_roll`, Telegram token, Firebase creds, MT5 login, Receiver host.
- Untracked in working tree (pre-existing, not this session): `.obsidian/`, `layer0/Flipped RSI
  Divergence Indicator.pine`, `logs/demo_chart_*.png`, `uv.lock`.

## Pick up here
If resuming the live system: run `/update layer2` for the 1% change and confirm the restart. If building:
start the prop kit via `docs/prop-leg/03-build-prompt.md` (build prop before personal).
