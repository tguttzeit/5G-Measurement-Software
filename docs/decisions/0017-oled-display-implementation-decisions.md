# 0017 — OLED display implementation decisions

## Context

[Issue #2](https://github.com/tguttzeit/5G-Measurement-Software/issues/2), scoped by
[decision record 0006](0006-oled-display-content.md). 0006 settled the architecture and content:
`Display` ABC + factory, lifecycle state from #7's state machine plus a quality summary reusing
#4's counters, and an extensible special-config-flag list. Two things came up while implementing
that weren't settled by 0006 and needed a call before writing code.

## Decision

**Lifecycle state, before #7 exists**: [#7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7)
(the `LOW_POWER_WAITING`/`ACTIVE_MEASURING` state machine 0006 designs the display's lifecycle
content against) is still open — there is no state machine yet to read from. Rather than block
this issue on #7 or invent #7's state names ahead of that issue actually being scoped, this adds
`RunPhase`: a plain mutable holder for a phase label, defaulting to `"starting up"`, that `main.py`
sets at each of its existing transitions (`"uploading pending data"`, `"collecting"`, `"finalizing
and uploading"`, `"done"`) - i.e. today's real linear flow, worded plainly rather than borrowing #7's
future state names for states that don't exist yet. The `Display`/content-builder side only ever
sees `RunPhase.get()`, a plain string, so nothing about the display's design or `StatusDisplayUpdater`
needs to change once #7 lands - only what calls `RunPhase.set()` does.

**Live refresh, not just phase-transition updates**: the quality summary changes continuously
during collection as measurements come in, not just at phase boundaries, so the display needs its
own refresh cadence. `StatusDisplayUpdater` runs on a background thread on a configurable interval
(`display.refresh_interval_s`), reading `RunPhase` and `RunStatusTracker` fresh each tick - the same
shape as `HeartbeatSender` and `KeepModemAliveSender`, for the same reason: the position-triggered
collection loop shouldn't have to drive it.

**Driver library**: `luma.oled` (with `luma.core` for the I2C serial interface, canvas rendering,
and device cleanup) for the SSD1306 itself. Not added to `pyproject.toml`: like `RPi.GPIO`, it's
Pi-hardware-specific and only needed when a real display is attached, so `SSD1306Display` imports
it only inside its methods and the Pi installs it separately (`uv pip install luma.oled`) when
`[display] enabled = true`.

## Reasoning

- Waiting on #7 was rejected: #7 is a large, separately-scoped autostart/battery-management
  feature, and blocking a physical-display driver on it entirely would leave #2 stalled for
  unrelated reasons. `RunPhase`'s plain-string interface means #7 landing later is a change to
  what gets set, not to `Display`, `StatusDisplayUpdater`, or the content builder.
- Inventing placeholder versions of #7's actual state names (e.g. a local `LOW_POWER_WAITING`) was
  rejected as more misleading than plain labels describing what's really happening today - there is
  no low-power waiting phase yet, so calling anything that would be confusing.
- A separate poll-and-render call at every `Collector` iteration was considered instead of a
  background thread, but rejected: it would tie the display's refresh rate to the movement-based
  capture rate (long idle stretches between captures would leave stale data on screen) rather than
  to a rate suited to a glanceable field display.
