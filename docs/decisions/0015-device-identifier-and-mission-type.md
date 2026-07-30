# 0015 — Device identifier and mission type metadata

## Context

[Issue #15](https://github.com/tguttzeit/5G-Measurement-Software/issues/15) — no device/vehicle
identifier anywhere in the system, filed as a low-priority placeholder since the fleet plan isn't
decided. This record settles a minimal design, and folds in a related metadata field (mission type)
the maintainer wanted added at the same time.

## Decision

**`device_id` config field, defaulting to `socket.gethostname()`** rather than requiring manual
assignment.

**`mission_type` config field** (`"ground"` or `"drone"`), defaulting to `"ground"` to match current
behavior — **metadata only, no behavior branching**. Nothing in `Collector`/`Uploader`/the quality
thresholds or state machine actually changes based on this value; it's descriptive data riding along
with the output, not a switch.

**Both fields added to `Datapoint` itself, not just the upload filename** — a filename-only
convention breaks if files ever get merged/re-ingested into one dataset on the backend; a field on
each record survives that regardless of file layout.

**`remote_dir`'s per-device-vs-shared question resolves itself**: once `device_id` disambiguates data
at the record level, whether `remote_dir` is shared or per-device becomes a free deployment choice,
not something this repo needs to enforce.

## Reasoning

- Hostname default over requiring manual config: Raspberry Pis typically already have distinct
  hostnames per device, so this gives real disambiguation for free if the fleet ever grows, with zero
  config burden for the current single-device case.
- `mission_type` as metadata-only, not a behavior switch: several other features already built this
  project — #4's quality thresholds (where a drone's weak signal might be the actual finding rather
  than a wasted run), #7's state machine (built specifically around ground-vehicle stop-and-go and
  garage departure), #3's movement-gated latency tests — all assume ground-vehicle patterns fairly
  deeply. Actually branching on `mission_type` would require revisiting each of those individually;
  out of scope here. This issue only exposes the field so it's available if/when that follow-up work
  happens.
- Both fields on `Datapoint`, not just the filename: consistent with the same reasoning already
  applied to the device identifier itself — record-level fields survive file consolidation or
  re-ingestion in a way a filename convention alone doesn't.