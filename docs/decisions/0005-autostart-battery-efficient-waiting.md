# 0005 — Autostart and battery-efficient waiting state machine

## Context

[Issue #7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7) — autostart on boot, then
wait battery-efficiently until the vehicle is actually moving before measuring. Confirmed while
tracing the code: `Collector.collect()` today opens the modem, opens GNSS, and starts the
`KeepModemAliveSender` heartbeat all *before* it waits for the first GPS fix — so full power draw
starts immediately on every boot regardless of whether the vehicle is moving. This record covers
the restructuring needed to fix that, which turned out to be bigger than "wait once at the start of
the day."

## Decision

**Two-state model for `Collector`**, not just a one-time start-of-day gate:

- `LOW_POWER_WAITING`: GNSS open and continuously polled (not duty-cycled — see reasoning), modem
  closed, no keep-alive running.
- `ACTIVE_MEASURING`: today's existing `collect()` behavior — modem open, keep-alive running,
  `position_threshold`-gated capture.

**Two idle tiers**, both driving state transitions:

- **Short idle** — today's `max_idle_time` (200s default), unchanged: modem stays open, keep-alive
  keeps running, just no new datapoints. Ordinary stop-and-go behaves exactly as it does today.
- **Long idle** — new, 15 minutes: transitions `ACTIVE_MEASURING` → `LOW_POWER_WAITING` (closes the
  modem, stops the keep-alive) instead of ending the session, since a day is one continuous run and
  the software has no way to resume within the same boot otherwise.

**Day-end**: a configurable time-of-day cutoff (e.g. 22:00), not an idle-duration threshold.
Shutdown-eligibility is `(currently in LOW_POWER_WAITING) AND (current time ≥ cutoff)` — it does not
forcibly interrupt an active measurement run if the clock crosses the cutoff mid-drive.

**Garage/home-position check**: optional extra AND-condition. If a garage coordinate is configured,
departure requires both movement-threshold clearance *and* distance from the garage coordinate; if
not configured, falls back to movement-only (today's logic unchanged).

**GNSS stays continuously on** during `LOW_POWER_WAITING`, not duty-cycled.

**SIM-unlock ([#5](https://github.com/tguttzeit/5G-Measurement-Software/issues/5)) sequencing**:
runs once at the `LOW_POWER_WAITING` → `ACTIVE_MEASURING` transition when the modem is (re)opened,
on the assumption — to be verified in the field, not yet certain — that SIM PIN unlock is a property
of the SIM chip's own state and persists across the AT/serial connection being closed and reopened,
as long as the SIM keeps power. If that assumption turns out wrong, SIM-unlock would need to run on
every transition instead of once per boot.

## Reasoning

- The two-state model (rather than a one-time start-of-day wait) exists because the "wait for
  movement" logic and the "vehicle stopped for a while" logic are the same problem: both are "no
  point running the modem right now." Treating them as separate mechanisms would duplicate logic and
  leave the original bug half-fixed — the session would still end outright on a long midday stop.
- Two idle tiers instead of just lowering `max_idle_time`: closing/reopening the modem has its own
  cost (QMI/PPP re-establishment, network re-registration time) — tearing it down on every short
  stop-and-go pause could plausibly cost *more* battery than it saves. Keeping short idle on today's
  behavior (modem stays open) avoids that; the long-idle tier only kicks in once the stop is long
  enough that the reconnection overhead is clearly worth it.
- Time-of-day cutoff instead of idle-duration for day-end: matches how the maintainer actually thinks
  about the workday ending (garbage collection routes run during business hours; 10pm is
  unambiguously "done for the day" regardless of how much idle time accumulated). This creates a real
  dependency on [#11](https://github.com/tguttzeit/5G-Measurement-Software/issues/11) (no RTC/clock
  sync) — a wrong system clock at boot would make this cutoff fire at the wrong wall-clock moment
  entirely, so #11 needs to be considered resolved (or at least the clock trusted) before this is
  safe to rely on.
- GNSS not duty-cycled: GNSS receivers typically need real time to reacquire a fix after being
  powered off (cold/warm start delay). Cycling it on/off during low-power waiting risks never
  acquiring a fix at all if the cycle is shorter than acquisition time — the actual power savings in
  this design come entirely from not running the modem during waiting, not from touching GNSS.
- Garage check as optional AND rather than required: forcing every deployment to configure a garage
  position would break for a route that doesn't always start from the same place; making it optional
  means the feature degrades gracefully to today's movement-only detection when not configured.