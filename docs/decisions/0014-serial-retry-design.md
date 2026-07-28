# 0014 — Transient serial failure retry design

## Context

[Issue #12](https://github.com/tguttzeit/5G-Measurement-Software/issues/12) — a momentary serial
read failure (vehicle vibration jostling a connector) currently ends the whole day's collection.
This became more consequential once [#10](https://github.com/tguttzeit/5G-Measurement-Software/issues/10)
settled its unified shutdown path: anything reaching `main()`'s top level is now unconditionally
fatal, and #10's own design explicitly assumes #12 handles retry logic before letting an exception
escape that far.

## Decision

**Classify by exception type, not by counting failures generically.** A timeout-style exception or a
short/empty read is transient — retry it. An exception indicating the port itself is gone
(`serial.SerialException` for device-not-found, permission-denied, or an I/O error on disconnect) is
a real fault — don't retry, let it propagate immediately.

**Retry budget**: 3 attempts, ~0.5-1s apart, both configurable (not hardcoded, matching this repo's
existing convention for tunable timing values).

**Shared retry helper**, used by both `Quectel` and `NMEASerial`, rather than duplicating the same
backoff loop in each class.

## Reasoning

- Classifying by exception type rather than a generic "retry N times regardless of why" avoids
  wasting retries on a fault that can't recover (a genuinely disconnected port won't start responding
  after a fixed delay) while still giving transient hiccups a real chance to clear.
- 3 retries / 0.5-1s: vehicle-vibration-caused connector jostles are expected to resolve within a very
  short window if they're going to resolve at all — a small, quick retry budget matches that, rather
  than a long backoff that would just delay reaching the "real fault" conclusion for a fault that
  isn't actually transient.
- Shared helper: both concrete classes need identical retry/backoff behavior around their serial
  reads; writing it once avoids the two implementations drifting apart over time.