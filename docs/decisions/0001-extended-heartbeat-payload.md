# 0001 — Extended heartbeat: what to classify where, and what to send

## Context

[Issue #4](https://github.com/tguttzeit/5G-Measurement-Software/issues/4) — the backend heartbeat
needs to signal more than liveness: whether the run is currently producing data correctly, and
whether the data it's producing is worth keeping. This record covers three decisions made while
scoping that out, before implementation started.

> **Superseded assumption**: this record originally assumed plain HTTP for the heartbeat transport.
> [0007](0007-remote-access-design.md) later repurposed this same channel to also carry remote
> config overrides and one-shot commands ([#8](https://github.com/tguttzeit/5G-Measurement-Software/issues/8)),
> which makes HTTPS with response authentication a hard requirement, not a nice-to-have — see 0007
> for the reasoning.

## Decision

**1. "Suspicious" is two independent signals, not one.**

- *Pipeline health* — is `Collector` currently failing to capture data despite the vehicle moving?
  Computed on-device, since it needs `Collector`'s internal state (raw GNSS fix stream vs. what
  actually got captured as a `Datapoint`) — the backend can't reconstruct this from data alone.
- *Data quality* — are the cell measurements themselves any good? Split further into:
  - *Legality*: is a field's value even possible (e.g. RSRP inside its valid 3GPP range, not a
    null/sentinel garbage value)? A binary, spec-derived check.
  - *Quality*: is the signal strong enough to be useful (e.g. RSRP above a configured threshold)?
    A judgment call, and mission-dependent — a drone mission in a dead zone might consider bad
    signal the actual finding, whereas for this vehicle mission, bad signal for the whole run
    likely means the run was wasted (bad route/antenna fault) and is worth flagging.

**2. Classification happens on-device; only compact counts go over the heartbeat.**

Both the legality check and the quality threshold run on-device, per `Datapoint`, as it's produced.
The heartbeat sends running counts, not raw `CellSample` fields:

```json
{
  "since_run_start": {
    "datapoints_total": 142,
    "good": 118,
    "bad": 20,
    "invalid": 4
  },
  "pipeline_broken": false
}
```

**3. Counts are cumulative since run start, not per-heartbeat-interval deltas.**

## Reasoning

- Sending raw `CellSample` fields (a dozen-ish numeric fields per sample) over the heartbeat was
  the original assumption, on the theory that centralizing all classification logic on the backend
  (once it exists, [#16](https://github.com/tguttzeit/5G-Measurement-Software/issues/16)) would let
  thresholds be retuned per mission without touching device config. Rejected: the modem's battery
  cost is dominated by radio-active time per transmission, not just raw byte count, and this
  heartbeat needs to fire often — a handful of small integers is meaningfully cheaper per tick than
  a dozen floats, especially multiplied across every sample in the window. The raw values still
  reach the backend eventually via the existing scp upload, which is the appropriate channel for
  bulk data.
- Trade-off accepted: quality thresholds now live in the device's `config.toml` (per RAT) rather
  than being purely backend-configurable. Retuning them means a device-side config change — this
  could later ride on the remote-access channel ([#8](https://github.com/tguttzeit/5G-Measurement-Software/issues/8))
  if adjusting without physically touching the Pi becomes necessary.
- Cumulative (not delta) counts: a lost heartbeat message with delta-style counts loses that
  window's data permanently, since the next message only covers its own window. Cumulative counts
  self-heal — any single received message reflects the whole run so far, so a dropped message just
  means a temporarily stale reading, not a hole in the data.
- Considered and rejected: time-windowed data quality checks (e.g. "good/bad over the last 60s").
  Doesn't fit this vehicle's actual operating pattern — a garbage truck spends long stretches
  stationary while loading, during which `Collector` produces zero new `Datapoint`s by design. A
  time-based window would empty out during every normal stop and misreport it as suspicious. A
  cumulative, whole-run percentage doesn't have this failure mode.
