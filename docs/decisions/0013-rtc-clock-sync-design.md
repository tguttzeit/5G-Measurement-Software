# 0013 — RTC/clock sync design

## Context

[Issue #11](https://github.com/tguttzeit/5G-Measurement-Software/issues/11) — no RTC/clock sync
means a Pi booting with a wrong system clock would mistimestamp an entire measurement day. This
record settles the design, reconciled with [#7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7)'s
state machine, which already named this issue as a dependency for its day-end time cutoff.

## Decision

**Parse `$GNZDA`/`$GPZDA` (Date & Time) sentences, in addition to the existing `$GNGGA`/`$GPGGA`
parsing** in `nmea_serial.py`. GGA alone only carries UTC time-of-day, no date.

**Set the system clock exactly once** — the first time a fix carrying a valid ZDA date+time arrives
during #7's `LOW_POWER_WAITING` state (GNSS is already continuously polled there). No repeated
re-syncing afterward.

**Mechanically**: a `set_system_clock()`-style function in `core/system.py`, shelling out via
`subprocess` (e.g. `sudo date -s ...`) — needs root, same pattern as the existing
`perform_shutdown()`'s `sudo shutdown -h now`.

**No separate "no trustworthy time" fallback policy.** If GNSS never gets a fix at all, that's
already the "no GPS fix ever" fatal-startup failure mode #10's ADR 0008 names — and since `Collector`
cannot capture any `Datapoint` without a fix in the first place, there's no path where data actually
gets recorded with an untrusted clock.

## Reasoning

- ZDA over GGA-only: a Pi without a battery-backed RTC can boot to a wrong *date*, not just a wrong
  time-of-day. Fixing only the time-of-day from GGA would leave the date wrong, which is arguably a
  worse failure mode than the status quo — a plausible-looking wrong-date timestamp is easier to miss
  during analysis than an obviously-wrong one. ZDA carries an unambiguous full date+time.
- Setting the clock once, not repeatedly: a single continuous measurement day doesn't run long enough
  for OS clock drift to matter at this project's timestamp precision — repeated re-syncing would add
  complexity for no practical benefit.
- Hooking into `LOW_POWER_WAITING` rather than a separate step: GNSS is already continuously polled
  there per [0005](0005-autostart-battery-efficient-waiting.md), so this is the earliest natural point
  available, well before any `Datapoint` capture begins, and it's exactly what #7's day-end cutoff
  needs resolved to be trustworthy.
- No proceed-with-bad-clock-and-flag-it policy: considered, but rejected as unnecessary — the
  "GNSS never gets a fix" scenario that would require such a fallback is already a named fatal-startup
  case in #10, and by construction no data capture can happen without a fix, so there's no scenario
  left where bad data with a bad clock actually gets produced.