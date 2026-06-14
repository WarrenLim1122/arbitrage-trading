# 10 — (SUPERSEDED) Prop-Halt Listener

> **This file is superseded.** The design changed (2026-06-14): personal no longer trades its own signal
> with a halt-listener bolt-on — it now **follows the prop system entirely** (opens the inverse hedge on
> every prop trade, mirrors closes, and honors K1–K5 halts).
>
> **See [`10-prop-follower.md`](10-prop-follower.md)** for the current, complete spec. Kept only as a
> redirect; do not build from this file.
