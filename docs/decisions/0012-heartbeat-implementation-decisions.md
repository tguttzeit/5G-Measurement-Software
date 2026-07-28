# 0012 — Extended heartbeat: pipeline-health definition and failure handling

## Context

[Issue #4](https://github.com/tguttzeit/5G-Measurement-Software/issues/4) — implementing the
extended heartbeat on top of [0001](0001-extended-heartbeat-payload.md) (what to classify where,
and what to send) and [0002](0002-heartbeat-quality-thresholds.md) (which metrics drive the
classification, and the starting thresholds).

Those two records left three things open that only surfaced once the code had to exist. Unlike the
other records here, these were **decided during implementation rather than worked out with the
maintainer beforehand** — they are written up so the reasoning is retraceable and can be overruled,
not because they were pre-agreed.

## Decision

**1. `pipeline_broken` means: the vehicle moved, the modem returned nothing, repeatedly.**

Specifically — the device cleared `position_threshold` and triggered a capture `N` times in a row
(`empty_captures_until_pipeline_broken`, default 3) and the modem yielded zero samples every time.
Any non-empty capture clears the streak.

Deliberately *not* included: a dead GNSS receiver. With no fixes there is no evidence the vehicle
is moving at all, so there is nothing to compare captured data against. That failure belongs to
[#12](https://github.com/tguttzeit/5G-Measurement-Software/issues/12) (transient serial failures)
and [#10](https://github.com/tguttzeit/5G-Measurement-Software/issues/10) (fatal-failure handling)
rather than being reported here as a broken pipeline.

**2. A broken heartbeat degrades; it never aborts the run.**

A non-`https://` URL is refused and logged at `ERROR`, and the run continues with the heartbeat off
for its duration. An unreachable backend logs at `WARNING` and the tick is skipped; the reporting
thread stays alive and keeps trying.

**3. LTE RSRQ's legality range tops out at +2.5 dB, not -3 dB.**

The legality check (0001's spec-derived binary check, as opposed to 0002's configured quality
thresholds) uses the *extended* RSRQ reporting range for LTE.

## Reasoning

- Pipeline health needs the raw-movement stream compared against what was actually captured, which
  is exactly what 0001 says the backend cannot reconstruct from data alone — so the collector is
  the only place it can be observed. A streak rather than a single empty capture, because one empty
  modem response is ordinary noise (a handover, a momentarily unreadable serving cell); three in a
  row while the vehicle keeps covering ground is not.
- Requiring movement before counting an empty capture also means the long stationary stretches this
  vehicle spends loading don't drift the device into a false `pipeline_broken` — the same failure
  mode 0001 rejected time-windowed quality checks for.
- Degrade-not-abort: the heartbeat is telemetry about the run, not part of producing the data. A
  typo in a URL or a backend outage taking down a garage-to-garage measurement day would invert the
  cost of the failure — the entire point of this channel is to *prevent* a wasted field day, per
  [0007](0007-remote-access-design.md). The loud `ERROR` is what makes the silence diagnosable;
  the OLED display ([#2](https://github.com/tguttzeit/5G-Measurement-Software/issues/2)) is a
  natural second place to surface it later.
- Rejecting a non-https URL rather than falling back to plain HTTP: 0007 makes authenticated
  transport a hard requirement for this channel because it will carry commands, so quietly sending
  over HTTP would be worse than not sending. This is the one misconfiguration that stays silent by
  design.
- The extended RSRQ range: the classic -19.5…-3 dB range would mark legitimate readings from a
  modem reporting the extended range as `invalid`, which is precisely the opposite of what the
  legality check is for — it exists to catch null/sentinel garbage, not to second-guess plausible
  measurements. Erring toward accepting a real reading and letting the configured quality threshold
  judge it keeps the two checks doing their separate jobs.
- Not decided here: what to do with the backend's HTTP *response*. It is currently ignored, since
  consuming it is [#8](https://github.com/tguttzeit/5G-Measurement-Software/issues/8)'s work (config
  overrides and one-shot commands) — which is why 0007's "HTTPS with response authentication"
  requirement lands in this issue as https-only for now, with the authentication half arriving when
  there is a response worth authenticating.
