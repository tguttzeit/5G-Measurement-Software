# 0006 — OLED display content and scope

## Context

[Issue #2](https://github.com/tguttzeit/5G-Measurement-Software/issues/2) — SSD1306 OLED display for
field status. Written originally against the old simple collection flow; this record updates its
scope to match #7's state machine and settles what the display actually shows.

## Decision

**Architecture**: `Display` ABC + factory pattern, matching `Modem`/`GNSSReceiver` — unchanged from
the issue's original suggested approach, no disagreement there.

**Content — lifecycle state plus a quality summary, not just one or the other**:
- Lifecycle state from [#7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7)'s state
  machine (`LOW_POWER_WAITING`, `ACTIVE_MEASURING`, uploading, shutting down, etc.), not the old
  "waiting for fix / collecting / uploading / shutting down" wording the issue originally used.
- A quality summary reusing the exact same cumulative counters
  [#4](https://github.com/tguttzeit/5G-Measurement-Software/issues/4)'s heartbeat already maintains
  on-device (`datapoints_total`/`good`/`bad`/`invalid`, plus `pipeline_broken`) — the display is a
  second consumer of that already-computed state, not a separate computation.

**Connection**: I2C (SDA/SCL, fixed GPIO2/3 on the Pi) — a shared bus, not a dedicated pin the way
the fan control or shutdown-signal GPIO are. Relevant to
[#14](https://github.com/tguttzeit/5G-Measurement-Software/issues/14)'s pin-coordination work: this
doesn't compete for a pin the way those do.

**Special-config indicator, generalized rather than GPS-only**: a dedicated section of the display
lists whatever testing/non-default config flags are currently active — starting with
[#17](https://github.com/tguttzeit/5G-Measurement-Software/issues/17)'s `gps_enabled = false`, but
built as an extensible list rather than a single hardcoded GPS check, so future testing/debug flags
can be surfaced without rewriting display logic each time. This satisfies #17's "loudly, visibly
flagged" requirement better than a log line would in the field, and generalizes to catch any other
case of a non-default flag being left on by accident before a real mission run.

## Reasoning

- Reusing #4's counters instead of a separate live-stats computation avoids duplicating logic that
  already exists for a different consumer (the backend heartbeat) — the display just reads the same
  running state whenever it refreshes.
- This creates a real dependency: #2's quality-summary content assumes #4's on-device classification
  exists. If #4 changes its counter shape, #2's display content follows.
- Lifecycle-state-only was considered (simplest, smallest surface area) but rejected as underusing a
  screen that's physically present in the field specifically for diagnostics — a bare "Measuring"
  message doesn't tell an operator whether the run is actually producing good data.
- Full live stats (raw RSRP/RSRQ/SINR values, per-sample detail) was also considered and rejected as
  overkill — that level of detail isn't glanceable on a small OLED, and #4 already reduces it to a
  simple good/bad/invalid tally for exactly this kind of at-a-glance use.