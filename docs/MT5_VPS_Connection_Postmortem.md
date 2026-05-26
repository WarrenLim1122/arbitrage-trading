# MT5 VPS Connection Postmortem — The `-10005` Saga

**Period:** ~2026-05 (multi-week) → resolved 2026-05-26
**Severity:** Live cutover fully blocked
**Resolution:** Found by Warren, after several wrong diagnoses by the agent
**Root cause:** Generic MetaQuotes MT5 installer ships without broker server endpoints, so the MT5 desktop client on the VPSes silently never attempted to connect to Fusion Markets / FundingPips accounts — even though the same accounts streamed perfectly on the mobile app and the same MT5 install streamed perfectly for the MetaQuotes-Demo built-in account.

> If you're hitting `-10005` again, START HERE: open MT5's **Journal** tab, select the failing account, and look for **Network** entries. If there are NONE, the server endpoints aren't configured → skip to "The actual fix" below.

---

## TL;DR

| | |
|---|---|
| What broke | `mt5.initialize(...)` returns `-10005 IPC timeout` for live broker accounts on both Windows VPSes |
| Wrong theory 1 | "The lib only IPCs with a terminal it self-launches" — TRUE, but not the active blocker |
| Wrong theory 2 | "Runtime account switching kills the IPC pipe" — TRUE, but not the active blocker |
| Wrong theory 3 | "Accounts aren't funded / not streaming" — FALSE. Both were funded the whole time |
| Wrong theory 4 | "Fusion Markets is IP-blocking the VPS" — FALSE. Mobile and demo worked from the same network |
| **Actual cause** | **Generic MetaQuotes MT5 install doesn't know broker server endpoints. When you select e.g. `FusionMarkets-Live` in the dropdown, the terminal silently never even tries to connect.** |
| The fix | Use the broker's branded MT5 installer (Option 1) **or** add the broker as a "company" via the existing MT5's `File → Open an Account` wizard (Option 2). Option 2 wires endpoints into the existing install without a new download. |
| Time cost | Weeks of debugging — could have been an hour with the right Journal-tab check on day one |

---

## The symptom

On both Windows VPSes (Vultr — VPS #2 personal / Fusion, VPS #3 prop / FundingPips):

1. Run `uv run python layer3/worker_personal.py` (or `_prop.py`)
2. After ~200 seconds: `MT5 init failed ((-10005, 'IPC timeout'))`
3. Retries forever

What WORKED on the same VPSes:
- MT5 desktop login to the built-in **MetaQuotes-Demo** account (`5050946734`) → streamed live data in ~5 seconds
- MT5 mobile app login to **both real broker accounts** → streamed live data fine
- The exact same `_worker_core.py` connection code, against MetaQuotes-Demo → connected in 6 seconds via IPC

What did NOT work on the same VPSes:
- MT5 desktop login to FusionMarkets-Live `459166` → "connected" in the title bar, but **prices frozen** at ~2015 values (EURUSD 1.10820, GBPUSD 1.56531). Chart history stuck at August 2024.
- MT5 desktop login to FundingPips2-SIM `12250900` → same: title showed account name, prices frozen.
- The `MetaTrader5` Python lib against either account → `-10005`

The matrix below was the actual signal we missed for weeks:

| Client | Account | Network | Result |
|---|---|---|---|
| MT5 mobile app | `459166` | iPhone cellular/WiFi | ✅ Live ticking prices |
| MT5 mobile app | `12250900` | iPhone cellular/WiFi | ✅ Live ticking prices |
| MT5 desktop (generic) on VPS | `5050946734` (MetaQuotes-Demo) | Vultr | ✅ Live ticking, IPC works |
| MT5 desktop (generic) on VPS | `459166` (Fusion-Live) | Vultr | ❌ Frozen prices, no IPC |
| MT5 desktop (generic) on VPS | `12250900` (FundingPips-SIM) | Vultr | ❌ Frozen prices, no IPC |

The `MetaQuotes-Demo works but the broker accounts don't` row is the smoking gun — both are running through the same install, same network, same VPS. Differences had to be in the **broker-specific configuration of the MT5 install**.

---

## The wrong diagnoses (and why they seemed right)

### Wrong theory 1 — "The lib only IPCs with a terminal it self-launches"

**Status:** TRUE constraint, but not the active blocker here.

We discovered through testing that `mt5.initialize(login=, password=, server=)` (passing creds directly) returns `-10005` because the MetaTrader5 Python lib needs to launch the terminal itself to set up the IPC pipe. Calling `mt5.initialize(path)` (self-launch) and letting the terminal use its saved-default account works.

**Why it felt like the answer:** Switching to self-launch DID fix something — it made the IPC pipe survive an account context. So the first fix attempt (commit `b22495f` two-phase auto-login) chased this and got partial traction.

**Why it wasn't enough:** Self-launch still requires the terminal's saved-default account to ACTUALLY connect to its broker. If the broker endpoints aren't configured in the install, self-launch just loads a non-streaming terminal and times out anyway.

### Wrong theory 2 — "Runtime account switching kills the IPC pipe"

**Status:** TRUE constraint, but not the active blocker.

`mt5.login()` or passing `login=` to `initialize()` to switch off the saved default does in fact kill the IPC. Verified by direct test (recorded in `mt5-python-integration-constraints.md`).

**Why it felt like the answer:** Combined with theory 1, it gave a clean architecture story (self-launch + hard guard, never switch). The story is correct — it's just not the cause of `-10005` in this specific case.

### Wrong theory 3 — "Accounts aren't funded / not streaming"

**Status:** FALSE.

The 2026-05-25 session concluded: "MetaQuotes demo streams, broker accounts show frozen 2015 prices → broker accounts must not be funded/activated." Captured in `handoff/SESSION-HANDOFF.md` (since corrected) and `mt5-python-integration-constraints.md` (since corrected).

**Why it felt like the answer:** Frozen prices on a connected-looking terminal really do match what an unfunded account looks like on some brokers. The frozen value (EURUSD 1.10820, identical across sessions, identical to historical August 2024 candles in the chart) felt like a stale-cache state from a broker that authenticated the login but withheld the data feed.

**Why it was wrong:** Warren proved this trivially in the next session — both accounts streamed on his phone the whole time. The accounts had been funded and active. The "frozen prices" were not stale-broker-data — they were **the local cache from a connection that never actually happened**. The terminal was showing whatever historical bars it had downloaded back when (possibly during initial install in 2024), and the lib's IPC timed out because there was no live data stream to gate on.

### Wrong theory 4 — "Fusion Markets is IP-blocking the VPS"

**Status:** FALSE.

After theory 3 was disproven (accounts stream on mobile), the next theory was that brokers were specifically blocking the Vultr VPS IPs from receiving live data — a common anti-VPS retail-broker behavior.

**Why it felt like the answer:** It explained why mobile worked (different IP) and desktop didn't (VPS IP). It also fit the "connected but no data" appearance of the MT5 desktop.

**Why it was wrong:** The Journal tab showed **zero Network entries** when selecting the broker accounts. An IP block would show connection attempts being rejected — "scanning network for access points" → "no connection". The actual log was empty: the terminal wasn't even *trying* to connect. That's a different failure mode entirely.

---

## The actual root cause (finally)

Warren noticed it by checking MT5's **Journal** tab while switching between accounts:

```
2026.05.26 12:53:17  Network  new demo account '5050946734' opened on MetaQuotes-Demo
2026.05.26 12:53:20  Network  '5050946734': authorized on MetaQuotes-Demo through Access Point SG 2
2026.05.26 12:53:22  Network  '5050946734': terminal synchronized with MetaQuotes Ltd.
2026.05.26 12:55:59  Network  '5050946734': disconnected from MetaQuotes-Demo
```

Then he selected `459166` (FusionMarkets-Live). **No new Network entries appeared.** Bottom-right status: `n/a` / `0/0 Kb`. The terminal wasn't disconnected — it had never attempted a connection in the first place.

The reason: this MT5 was installed from `metaquotes.com`'s generic installer. The generic installer ships with MetaQuotes' own server endpoints baked in (which is why MetaQuotes-Demo connects in 5 seconds), but it does NOT ship with broker-specific server endpoints. When you "add" a server like `FusionMarkets-Live` by typing it into the dropdown, MT5 finds the name in MetaQuotes' global server registry but never actually receives valid endpoints to dial. Login attempts result in zero TCP activity.

This is essentially a config-not-installed failure mode that masquerades as a connection failure.

---

## The actual fix (Warren's discovery)

Two ways to wire the broker endpoints into the MT5 install. Both verified working on 2026-05-26.

### Option 2 — `File → Open an Account` wizard (RECOMMENDED — no download)

In the existing generic MT5 install on the VPS:

1. `File → Open an Account`
2. The **"List of companies"** page appears — this is the screen Warren says was missed for weeks.
3. **Select the broker's company** from the list (or type the broker domain in "Find your company"):
   - **Fusion Markets** → choose **`Fusion Markets Pty Ltd`** (3rd entry on the list as of 2026-05-26)
   - **FundingPips** → choose **`FundingPips Corp (2)`** (2nd entry — the `(2)` suffix matches server `FundingPips2-SIM`)
4. `Next` → **"Connect with an existing trade account"**
5. Enter login + password → select matching server from dropdown → **TICK "Save password"** → `Finish`
6. Wait until prices stream (bottom-right green + ticking rate like `22.0 / 0.0 Mb`)
7. Close MT5 — the worker will self-launch its own instance.

This wires the broker's actual server endpoints into the existing install. No new download, no second MT5 program to manage.

**Note:** Option 2 is desktop-only. The iPhone MT5 app handles broker selection via a different mechanism and was never affected by this bug — that's why phone always worked.

### Option 1 — broker-branded installer (fallback)

If Option 2's company isn't in the list, download the broker's MT5 installer from their portal:

- **Fusion Markets:** https://fusionmarkets.com/Platforms/Metatrader-5 → MT5 for Windows
- **FundingPips:** log in to fundingpips.com dashboard → Platforms / Downloads → MT5 for Windows

The branded installer ships with the broker's endpoints baked in. Installs into a folder like `C:\Program Files\Fusion Markets MetaTrader 5\` (note the non-standard prefix — Layer 3 code's glob handles this).

After install: `File → Login to Trading Account` → enter creds → TICK "Save password" → Login.

---

## Diagnostic checklist (use FIRST next time)

| Bottom-right corner of MT5 | Journal tab entries when account selected | Diagnosis |
|---|---|---|
| 🟢 Green + kb/s + prices ticking | `authorized on ... through Access Point ...` | ✅ Working — proceed to deploy |
| `n/a` / `0/0 Kb` | **ZERO Network entries** | **Server endpoints not configured** → Option 2 (or Option 1 if company missing) |
| `n/a` | `authorization failed` / `invalid account` | Wrong password or wrong server name |
| `n/a` | `no connection` after `scanning network for access points` | IP-blocked from this VPS → contact broker support |
| 🔴 `No connection` | `scanning network for access points` repeating | Network/firewall issue between VPS and broker |

**The Journal tab is the single highest-signal piece of evidence.** Check it before any other diagnostic.

---

## Why this wasted weeks

A handful of reasons worth remembering:

1. **The agent kept chasing constraints that were TRUE but not the active blocker.** Theories 1 and 2 (self-launch / no account switching) are real MT5 quirks documented in the lib's behavior. They got conflated with the actual problem because both produce `-10005`. **Lesson:** when a fix "kind of works", be suspicious — partial fixes can mask the real issue and let you build wrong mental models.

2. **The Journal tab was not checked early enough.** Empty Network entries are an immediate, definitive signal that the terminal isn't even attempting a connection — which rules out feed-side, broker-side, and account-side issues in one shot. The agent jumped to broker support messages and IP-block theories before checking the most basic local-state diagnostic.

3. **The "MetaQuotes-Demo works, broker accounts don't" matrix wasn't taken seriously enough.** From day one this matrix existed in the data. It's only consistent with one explanation: the broker accounts have a different config from the demo account in the install. But it was treated as evidence for feed-side theories instead.

4. **The previous session's confident-sounding diagnosis ("not funded/activated, broker-side") was stored in the handoff and the auto-memory.** That made the WRONG conclusion the priority of the next session. **Lesson:** confidence in a handoff is load-bearing — write down what you're sure of vs. what's still hypothesis.

5. **Generic-vs-branded MT5 is a known issue in the retail-broker world** but isn't surfaced in MetaQuotes' own docs or the `MetaTrader5` Python package docs. The signal that should have triggered it — "I downloaded MT5 from metaquotes.com, not my broker" — was knowable from session 1. Warren remembered this in passing late in the resolution session and asked "is that the problem?" — which it was.

---

## Code state after resolution

Three commits on `main`:

- `72b3921` — `_worker_core._connect_mt5()` rewritten: self-launch `mt5.initialize(terminal_path, timeout=120_000)` + hard guard `account_info().login == MT5_LOGIN` (fatal exit on mismatch). New `MT5_TERMINAL_PATH` env. `MT5_PASSWORD` / `MT5_SERVER` downgraded to optional (reference only).
- `75f55f5` — glob broadened from `C:\Program Files\MetaTrader*\` to `C:\Program Files\*MetaTrader*\` so broker-branded folder names (e.g. `Fusion Markets MetaTrader 5\`) are found. Warns on ambiguous matches.
- `6b0c462` — CLAUDE.md: added `VPS MT5 Setup` section (the two workflows above); Layer 3 status flipped to ✅ UNBLOCKED.

The connection rewrite (theories 1 and 2) was still worth shipping — it makes the worker robust against future account-switch attempts and gives a hard guarantee we never trade on the wrong account. But it was not what unblocked the live cutover.

---

## What to do if you see `-10005` again

1. Open MT5 on the VPS. Check the **Journal** tab.
2. Select the failing account in Navigator.
3. Watch the Journal for 5-10 seconds.
4. Use the diagnostic table above to identify which failure mode you're in.
5. If "ZERO Network entries" → go to **VPS MT5 Setup** in CLAUDE.md, do Option 2.

Don't re-debug feed/funding/IP theories. They were wrong here and will be wrong next time unless the symptom matrix is genuinely different. The matrix that points at server-endpoint config is: **mobile works, MetaQuotes-Demo on the VPS works, broker accounts on the VPS don't**.
