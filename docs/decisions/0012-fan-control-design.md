# 0012 — Fan control design

## Context

[Issue #6](https://github.com/tguttzeit/5G-Measurement-Software/issues/6) — case fan control based
on device load. This record settles the four open design questions from the issue.

## Decision

**Control granularity**: on/off with hysteresis, not PWM speed control.

**Load signal**: CPU temperature via `/sys/class/thermal/thermal_zone0/temp`, not CPU utilization.

**Where it runs**: its own independent background thread, same shape as
`core/keep_modem_alive_sender.py::KeepModemAliveSender`, running for the whole process lifetime —
**not** tied to [#7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7)'s
`LOW_POWER_WAITING`/`ACTIVE_MEASURING` state machine.

**Hysteresis**: two distinct configurable thresholds, not one — fan on at 70°C, fan off at 60°C as
starting defaults, both in a new optional `[fan]` config section (disabled by default, matching
`collector`/`logging`/`system`).

## Reasoning

- On/off over PWM: satisfies both goals in the issue (prevent overheating, avoid unnecessary
  noise/power when idle) with a simpler API (plain digital GPIO output vs. `RPi.GPIO`'s separate PWM
  class and a PWM-capable pin requirement). No evidence yet that binary control is insufficient in
  practice — PWM stays a future option if it turns out too crude.
- Temperature over utilization: temperature is the thing actually being protected against; CPU
  utilization is only a proxy that doesn't account for ambient/enclosure airflow, which matters more
  for an enclosed field case than raw CPU load. Also simpler to implement and test — one sysfs file
  read, no extra library.
- Independent thread rather than tied to #7's state machine: the CPU keeps running and can still
  overheat regardless of whether `Collector` is actively measuring or in low-power waiting — the
  modem/GNSS being closed during low-power waiting says nothing about the Pi's own thermal state.
  Coupling fan control to that state machine would leave it blind during exactly the periods it might
  still need to react.
- Two thresholds instead of one: a single threshold would rapidly toggle the fan on/off right at the
  boundary as temperature hovers around it. Distinct on/off thresholds with a gap avoid that. 70°C/60°C
  chosen as a starting default with real margin below the Pi's typical ~80-85°C thermal-throttling
  point, configurable for retuning once real enclosure/field data is available.