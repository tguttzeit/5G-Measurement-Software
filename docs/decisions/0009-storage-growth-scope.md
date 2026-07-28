# 0009 — Storage growth: scoped down after checking real numbers

## Context

[Issue #13](https://github.com/tguttzeit/5G-Measurement-Software/issues/13) — unbounded local
storage growth if the upload backlog can't be flushed. The issue itself asked for real numbers
before deciding whether this needs a mitigation system at all. This record captures those numbers
and the resulting scope decision.

## Decision

**No cap/eviction policy.** Scope reduced to two cheap additions:

1. Report pending-file count / disk usage as one more stat on
   [#4](https://github.com/tguttzeit/5G-Measurement-Software/issues/4)/[#8](https://github.com/tguttzeit/5G-Measurement-Software/issues/8)'s
   existing heartbeat channel — free visibility, no on-device policy needed.
2. A simple loud-warning tripwire: log prominently if free space drops below a fixed safety margin
   (e.g. 500MB). Not a cap, not an eviction policy — just an early warning in case the estimate below
   turns out wrong.

Explicitly **not** building: free-space checks before every write, a retained-file cap, or a
drop-oldest/refuse-new-runs policy — all originally floated in the issue, now considered
over-engineering given the numbers below.

## Reasoning

- **Confirmed with the maintainer**: the deployed Pi has a 128GB SD card, and connectivity gaps are
  expected to be infrequent/short since network availability gets tested regularly.
- **Rough size estimate** (from the actual dataclasses, not measured): a `Datapoint` (timestamp +
  `GNSSFix`/`Position` + `CellSample`'s ~12 fields) serialized via `json.dump(..., indent=4)` is
  roughly 400-600 bytes. At the default `position_threshold=15m` and a plausible day's driving
  (50-100km), that's on the order of a few MB up to maybe 10-20MB per day.
- At the high end of that estimate, a full year of *undelivered* data would be roughly 7GB — under
  6% of the card's capacity. Given expected connectivity is frequent, actually accumulating anywhere
  near that is very unlikely.
- Building a cap/eviction system for a risk this unlikely would add real complexity (deciding what
  "drop oldest" means for data correlated with a specific route/day, deciding whether to refuse a new
  run outright) for a problem the numbers say probably won't occur. A cheap tripwire plus visibility
  captures nearly all the practical value (catching a genuinely anomalous situation — a runaway bug,
  or a much-longer-than-expected outage) without the complexity cost of a policy for something
  unlikely to ever trigger it.