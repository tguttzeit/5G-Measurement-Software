# 0002 — Heartbeat data-quality metric and thresholds

## Context

[Issue #4](https://github.com/tguttzeit/5G-Measurement-Software/issues/4) — follow-up to
[0001](0001-extended-heartbeat-payload.md), which established that each `Datapoint` gets classified
`good`/`bad`/`invalid` on-device. This record covers which `CellSample` fields drive that
classification, and what "good" means numerically.

## Decision

**Metric**: RSRP, RSRQ, and SINR together, not RSRP alone.

**Combination logic**: a sample is `good` only if all three clear their threshold; if any one
doesn't, it's `bad`.

**Starting thresholds** (per RAT — LTE and NR configured separately in `config.toml`, same starting
values for both until field data suggests otherwise):

| Metric | "Good" threshold |
|---|---|
| RSRP | ≥ -100 dBm |
| RSRQ | ≥ -11 dB |
| SINR | ≥ 0 dB |

These are commonly-cited reference cutoffs (the kind used as default tiers in drive-test tools),
explicitly not yet validated against this project's own field data — expected to be retuned once
real measurements are collected.

## Reasoning

- RSRP-only was the simpler starting option (one threshold to tune) but was rejected: it only
  captures raw signal strength, missing the case of strong RSRP with an unusable link due to
  interference or cell congestion.
- RSRQ was initially unclear in value versus SINR, since both are "quality" metrics — but they
  capture different things. SINR reflects noise/interference at the receiver; RSRQ is derived from
  RSRP relative to total received power (RSSI), so it also reflects cell *loading* — a busy/congested
  cell can show poor RSRQ even with clean SINR. Since `CellSample` already carries RSRQ as a field
  and reading it costs nothing extra (same AT query already returns it), there was no reason to
  leave it out.
- AND-combination (not majority vote or a weighted score) chosen because weak signal, a congested
  cell, and a noisy link are each independently sufficient to make a measurement not very useful for
  a coverage/quality study — any one of the three being poor is reason enough to call the sample
  `bad`, rather than letting two good metrics outvote one bad one.
- Thresholds live in `config.toml` per RAT (not hardcoded) specifically so they can be retuned from
  real field data without a code change, consistent with 0001's acceptance that quality logic runs
  on-device rather than centrally on the backend.