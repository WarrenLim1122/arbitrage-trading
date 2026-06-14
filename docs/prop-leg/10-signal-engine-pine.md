# 10 — Signal Engine (Pine indicators) + webhook contract

This system has its **own** signal engine: three TradingView indicators that detect 1D/15m structure and
fire a **breakout-fade** entry — a countertrend trade with a **tight stop just beyond the breakout
extreme** and a **far target** at the opposite projection (high reward-to-risk, RR ≈ 3.7). All three emit
the same 14-field webhook (`05 §1`) to the Receiver. **Naming rule (`00`): in the built Pine, name
variables and comments for their own role (stop / target / breakout extreme) — never reference another
system or use flip/inverse/mirror/opposite.**

## The strategy (self-contained description)
- **Bullish breakout detected →** the system enters **SHORT** (fading the breakout): it expects the move
  to fail. Stop = just beyond the breakout high (the near structural level above entry). Target = the far
  projected level below entry. Tight stop, far target.
- **Bearish breakdown detected →** the system enters **LONG**: stop just beyond the breakdown low (near,
  below entry); target at the far projected level above entry.
- This profile means the system **wins big when the breakout fails** and takes small, fixed-risk losses
  when it doesn't — which is why Phase 1 sizes a fixed risk over the tight stop and lets the take-profit
  carry the stage reward (`02 §2`).

## Three indicators to build
| Built file (suggested) | Detection reused from reference | Emits |
|---|---|---|
| `1D-15m Breakout-Fade.pine` | `layer0/1D-15m Breakout INDICATOR.pine` | fade entry on the 1D/15m breakout |
| `RSI-Divergence-Fade.pine` | `layer0/Flipped RSI Divergence Indicator.pine` | fade entry on the RSI-divergence signal |
| `Nadaraya-Watson-Fade.pine` | `layer0/Nadaraya-Watson Webhook INDICATOR.pine` | fade entry on the NW-band signal |

You **reuse the detection math** (breakout / divergence / NW band logic — keep ~95% of it) and change
only the **emitted direction and the placement of stop vs target**.

## The concrete transformation (per indicator)
The reference breakout indicator detects a bullish setup and computes `entry_px`, `sl_px` (the far level),
`tp_px` (the near level), then emits a payload with `"signal":"LONG"` (reference lines ~185–186); it has a
symmetric bearish branch emitting `"signal":"SHORT"` (~211–212). For this system, in each branch:

```
Bullish-setup branch → emit a SHORT entry:
    entry  = entry_px
    stop   = the NEAR level above entry        (the breakout extreme + buffer)
    target = the FAR level below entry         (the downside projection)
    signal = "SHORT"
Bearish-setup branch → emit a LONG entry:
    entry  = entry_px
    stop   = the NEAR level below entry        (the breakdown extreme − buffer)
    target = the FAR level above entry         (the upside projection)
    signal = "LONG"
```
i.e. the **near structural level becomes the stop** and the **far level becomes the target** (the high-RR
fade box). Recompute `sl_pips` and `rr_ratio` from the new stop. Keep all other webhook fields as the
reference emits them (`timeframe`, `m15_swing_*` defaulted to 0, `pip_type`, etc.).

> In the **built** Pine, write this in the indicator's own voice — e.g. `stopPx` = breakout high + buffer,
> `targetPx` = measured-move projection — with comments describing the fade strategy directly. Do **not**
> carry over any comment that frames it relative to another setup/system.

## Webhook payload (emit exactly this — `05 §1`)
```
{"signal":"SHORT","ticker":"<syminfo.ticker>","timestamp_ms":<time>,"timeframe":"15m",
 "entry":<entry>,"sl":<stop>,"tp":<target>,"sl_pips":<sl_pips>,"rr_ratio":<rr>,
 "order_type":"MARKET","daily_trend":"<BULLISH|BEARISH>","m15_swing_high":<or 0>,
 "m15_swing_low":<or 0>,"pip_type":"<pip_type>"}
```
- `alert(payload, alert.freq_once_per_bar_close)` on the entry bar.
- **Never** let a numeric field be `na` → Pine `str.tostring(na)` = `"NaN"` = invalid JSON = 422. Default
  numerics to 0.
- One chart per pair; the same 14-field contract the Receiver validates.

## Build notes
- These are **indicators** (alerts), not strategies — the reference `*INDICATOR.pine` files are the base,
  not the `*STRATEGY.pine`.
- Plot the entry markers (`plotshape`) consistent with the emitted direction so the chart reads correctly
  on its own.
- The Pine files live in the new repo (e.g. `pine/`); they are pasted into TradingView and wired to the
  public `/signal` webhook at deploy (`09`).
