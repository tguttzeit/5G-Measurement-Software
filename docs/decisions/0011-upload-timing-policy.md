# 0011 — Upload timing tied to #7's state transitions

## Context

[Issue #18](https://github.com/tguttzeit/5G-Measurement-Software/issues/18) — when `Uploader`
actually uploads pending data, beyond today's fixed start/end-of-run points. Split off from
[#9](https://github.com/tguttzeit/5G-Measurement-Software/issues/9), broadened to a general policy.
This record settles it now that [#7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7)'s
state machine exists to hang the timing off of.

## Decision

**Upload attempts happen at #7's state transitions, not a separate schedule**: right before the
modem closes on the `ACTIVE_MEASURING` → `LOW_POWER_WAITING` transition (whether triggered by long
idle or the day-end cutoff), and right after it opens on the reverse transition. This replaces
today's "exactly twice per run" (start/end of `main()`), which no longer maps cleanly onto a day
that can now have many active/waiting cycles.

**Ordering**: oldest-first, unchanged from today's lexicographic/chronological
`sorted(self._upload_dir.glob("*.json"))`.

**Recovered/old data**: no special tagging or held-back handling — treated as just another pending
file through the same mechanism.

## Reasoning

- State-transition-triggered uploads are free: at both transition points, the modem/network
  connection is already open for another reason (about to close, or just opened) — so attempting an
  upload there costs no additional radio wake-up beyond what's already happening. A separate periodic
  upload timer was considered and rejected for the same reason #3/#4 rejected extra polling elsewhere
  in this project: it would be new, avoidable battery cost.
- Oldest-first kept as-is: no strong reason to prefer newest-first surfaced, and
  [0009](0009-storage-growth-scope.md) already established that unbounded local accumulation isn't a
  real pressure given the actual storage numbers — so there's no "prioritize fresh data before we run
  out of room" argument to make here.
- No special recovered-data handling: distinguishing recovered-from-a-crash data from a normal
  upload would need provenance metadata that doesn't exist yet — that's
  [#15](https://github.com/tguttzeit/5G-Measurement-Software/issues/15) (no device/vehicle
  identifier), which is itself low priority and undesigned. Building special-case handling ahead of
  the infrastructure that would support it is the same premature-complexity trap
  [0009](0009-storage-growth-scope.md) avoided for #13.