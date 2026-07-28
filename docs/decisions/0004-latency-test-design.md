# 0004 — flent latency test design

## Context

[Issue #3](https://github.com/tguttzeit/5G-Measurement-Software/issues/3) — active latency and
latency-under-load checks (via flent) against a university test server, correlated with cellular/GPS
data. This record covers the design points worked out with the maintainer.

## Decision

**Two independent cadences**, both configurable in `[latency_test]`:
- Baseline (plain ping-latency): every 60s.
- RRUL (latency-under-load): every 15 minutes.

**Movement-gated**: a periodic thread (same shape as `KeepModemAliveSender`) checks in on its own
timer; each tick, it skips running the test if the vehicle isn't currently moving, and retries at
the next tick — no separate tracking of cumulative moving-time.

**Distilled result, not flent's raw output**: extract a small summary (baseline RTT, RTT-under-load,
plus whatever percentiles turn out to matter) rather than storing flent's full verbose per-flow
output.

**Separate output file, not embedded in `Datapoint`**, with its own interval semantics:

```python
@dataclass
class LatencyResult:
    test_type: str          # "baseline" or "rrul"
    start_timestamp: str
    end_timestamp: str
    start_fix: GNSSFix
    end_fix: GNSSFix
    baseline_rtt_ms: float | None
    rtt_under_load_ms: float | None
    # ...distilled flent metrics
```

Written to its own file (e.g. `latency_<timestamp>.json`), uploaded through the existing `Uploader`
mechanism unchanged (it already just globs any `*.json` file in `upload_dir`).

**External dependency, not resolved here**: flent needs something running server-side (netperf/irtt,
depending on test type) on the university's test server — same category of "needs confirming with
the university" caveat as [#8](https://github.com/tguttzeit/5G-Measurement-Software/issues/8)'s
WireGuard question. Tracked as a blocker, not something this repo's code can resolve.

## Reasoning

- Two cadences instead of one shared interval: a baseline ping is cheap; RRUL deliberately saturates
  the link for tens of seconds and is real data/battery cost. Treating them identically would force
  a single interval that's either too aggressive for RRUL or too sparse for baseline.
- Movement-gating: an RRUL run somewhere the vehicle isn't moving doesn't add route coverage, and
  this vehicle spends real time stationary (loading stops) — running expensive tests during those
  stops would waste battery/data for no measurement value. Consistent with `Collector` already
  gating capture on movement.
- Rejected embedding `LatencyResult` into `Datapoint` as a nullable field, for three concrete reasons
  worked through with the maintainer:
  1. Cadence mismatch — `Datapoint`s are created every time `position_threshold` clears (every few
     seconds while driving), far more often than latency tests run, so the field would be `null` on
     the overwhelming majority of records.
  2. Cardinality mismatch — `_capture_datapoints()` already creates multiple `Datapoint`s from a
     single fix (one per `CellSample`/RAT); a `LatencyResult` becoming ready at that moment has no
     unambiguous single `Datapoint` to attach to.
  3. Semantic mismatch — a `Datapoint`'s timestamp represents an instant (one GPS fix, one cell
     reading, simultaneously); an RRUL run spans tens of seconds of real time and, given
     movement-gating, real distance too. It's an interval measurement, not a snapshot.
- Start/end fix+timestamp pair (rather than a single fix) directly resolves reason 3 above — it
  represents "the vehicle went from here to there while this test ran," which a single point-in-time
  fix cannot.