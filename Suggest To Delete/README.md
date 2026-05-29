# Suggest To Delete

Staging folder for files Claude flagged as deletable during the 2026-05-29
reorg. Warren reviews this folder and trashes whatever he doesn't want to
keep. When he's done, this whole folder can be removed:

```
git rm -rf "Suggest To Delete/"
git commit -m "chore: drop reviewed delete candidates"
```

Anything Warren wants to restore can be `git mv`d back to its original
location (or `mv`d for untracked items).

---

## Tracked files (would re-enter git history if restored)

| Path inside this folder | Original location | Why suggested |
|---|---|---|
| `docs/AI_Workflow.md` | `docs/AI_Workflow.md` | Mentions stale tooling (`dd_floor.json`, sonnet 4.6). No code/docs reference it. Keep only if it's a portfolio piece. |
| `docs/superpowers/plans/2026-05-16-phase1-strategy.md` | `docs/superpowers/plans/...` | Pre-implementation plan. Phase 1 strategy is now live in `layer2/phase1_strategy.py`. |
| `docs/superpowers/specs/2026-05-16-phase1-strategy-design.md` | `docs/superpowers/specs/...` | Historical design spec; served its purpose. |
| `layer0/TEST-ONLY 15m Loop INDICATOR.pine` | `layer0/...` | Filename says TEST-ONLY. CLAUDE.md lists only `1D-15m Breakout INDICATOR.pine` as live. Keep only if it's attached to a TradingView chart somewhere. |
| `scripts/backfill_journal.py` | `scripts/backfill_journal.py` | Hardcoded for a single past XAUUSD trade (ticket `8520846485`, 2026-05-07). Single-use script, already executed. |

## Untracked files (gitignored — safe to delete from disk only)

| Path inside this folder | Why suggested |
|---|---|
| `.DS_Store` | macOS Finder metadata (already in `.gitignore`). |
| `skills-lock.json` | Lockfile for the broken `.claude/skills/skill-creator` symlink (also moved here). |
| `.claude/skills/skill-creator` | Broken symlink → `../../.agents/skills/skill-creator` (target doesn't exist locally). Real skill lives at the global `~/.agents/skills/skill-creator`. |
| `logs/layer2_2026-05-16.log` | 366 B stale dev-machine log. Real logs live on VPS #1. |
| `logs/layer2_2026-05-23.log` | 0 bytes. |
| `logs/layer2_2026-05-24.log` | 0 bytes. |
| `logs/layer2_2026-05-28.log` | 0 bytes. |
| `logs/layer3_worker_2026-05-23.log` | 0 bytes. |
| `logs/layer3_worker_2026-05-28.log` | 0 bytes. |

> `logs/layer2_2026-05-29.log` and `logs/layer3_worker_2026-05-29.log`
> were intentionally NOT moved — they were created today and may be
> touched by a local process Warren has running.
