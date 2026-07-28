# 0003 — GPS-disable testing mode

## Context

[Issue #17](https://github.com/tguttzeit/5G-Measurement-Software/issues/17) — a config-driven way
to disable GPS for testing the rest of the pipeline without real GNSS hardware, defaulting to
enabled. This record covers the three open design points from that issue.

## Decision

**Config mechanism**: a new `[collector] gps_enabled: bool = True` flag, plus
`gps_disabled_poll_interval_s: float = 5.0` — not a new `GNSSReceiver` factory type/key.

**Placeholder marker**: add `placeholder: bool = False` to `GNSSFix` only (not duplicated onto
`Datapoint`, which already carries the whole `GNSSFix`). `Position.latitude`/`longitude` stay as
required floats (fixed dummy values when placeholder) rather than becoming `Optional`.

**Movement-gating bypass**: when `gps_enabled` is `False`, `Collector` captures on the fixed
`gps_disabled_poll_interval_s` timer (default 5s) instead of movement-threshold gating.

**Test fakes stay separate**: the existing scripted `FakeGNSSReceiver` in `test_collector.py` is
not replaced by or merged with the new placeholder implementation.

## Reasoning

- Factory-based approach (a new `GNSSReceiver` type key like `"disabled"`) was rejected because
  `GnssConfig`'s `port`/`baud_rate`/`timeout` are all required fields with no defaults — using the
  factory would force meaningless dummy serial settings into `config.toml` just to satisfy the
  dataclass. The flag approach needs no `[gnss_receiver]` changes at all, and `CollectorConfig` is
  already all-optional-with-defaults, so a new flag fits that existing pattern directly.
- `(0.0, 0.0)` ("Null Island") was the original concern in the issue, to be solved by making
  `Position`'s fields `Optional`. Simpler alternative: an explicit `placeholder: bool` flag makes
  the coordinate values themselves irrelevant once checked — consumers check the flag, not the
  coordinates — so `Position` doesn't need a schema change (`latitude`/`longitude` stay required
  floats) to solve the ambiguity.
- Movement-gating bypass is necessary, not optional: traced through `Collector._has_moved_enough()`
  — a placeholder that always returns the same fixed position produces a `haversine()` distance of
  `0` on every read after the first, so movement clears the threshold exactly once (the unconditional
  first-call `True`) and never again, meaning the session would capture a single datapoint and then
  idle out. A fixed-interval capture path is needed specifically because the threshold-based
  approach silently breaks down with a non-moving placeholder fix.
- 5 second default poll interval: fast enough for practical manual bench/dev testing without
  hammering the modem in a tight loop; picked pragmatically since this only matters for local
  testing, not the field-deployed default behavior (which stays `gps_enabled = True`).
- Test fakes: `FakeGNSSReceiver` needs to script arbitrary sequences (including `None` fixes, to
  exercise idle-timeout and lost-fix edge cases) for a wide range of `Collector` test scenarios; the
  production placeholder implementation only needs to return one fixed value, driven by config, with
  no scripting. Merging them would either constrain the test fake's flexibility or overcomplicate the
  production implementation for no actual benefit.