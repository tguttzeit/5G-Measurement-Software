# 0007 — Remote access: config overrides and one-shot commands

## Context

[Issue #8](https://github.com/tguttzeit/5G-Measurement-Software/issues/8) — remote access to the
device via the backend. This record settles the mechanism, scope, and security posture, superseding
the original issue's "polling vs. WireGuard tunnel" framing.

## Decision

**No new channel — piggyback on [#4](https://github.com/tguttzeit/5G-Measurement-Software/issues/4)'s
existing heartbeat.** The backend's HTTP response to each periodic heartbeat POST carries any
pending config overrides and/or one-shot commands for the device. No separate polling loop, no
WireGuard tunnel, no inbound listening port on the device ever.

**Two categories of remote operation, not one flat command list:**

- **Config overrides (persistent)**: written to a local, gitignored override file (e.g.
  `config_overrides.toml`) merged over the base `config.toml` at load time — never written back into
  the tracked `config.toml` itself (same reasoning as [#5](https://github.com/tguttzeit/5G-Measurement-Software/issues/5)'s
  SIM-PIN-in-git concern). Persists across the boot-per-day cycle from #7.
- **One-shot commands (not persistent)**: execute immediately on receipt, nothing to persist —
  force-upload-now, shutdown-now, SIM-reset-now, fan-override-now.

**Command/override set is governed by one principle**: include anything that could prevent an
entire field day's drive from being wasted (confirmed with the maintainer) — data-quality/timing
thresholds ([#4](https://github.com/tguttzeit/5G-Measurement-Software/issues/4)'s RSRP/RSRQ/SINR
cutoffs, [#3](https://github.com/tguttzeit/5G-Measurement-Software/issues/3)'s test intervals,
[#7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7)'s idle/day-end thresholds),
force-upload, shutdown-now, SIM-reset ([#5](https://github.com/tguttzeit/5G-Measurement-Software/issues/5)),
fan-override ([#6](https://github.com/tguttzeit/5G-Measurement-Software/issues/6)).

**Explicitly excluded from remote override — allow-listed, not fully generic**: anything about the
device's identity or how it reaches the network — `upload_host`, `upload_user`, `remote_dir`, GPIO
pin assignments, serial ports. These stay local-config-only.

**Security hardening required**: this channel now carries executable commands and config mutations,
not just outbound telemetry — the heartbeat transport must be HTTPS with response authentication
(not the plain HTTP loosely assumed in [0001](0001-extended-heartbeat-payload.md)), and command
dispatch should use the same factory-dict style already established for `Modem`/`GNSSReceiver`/
`Display`, not bespoke per-command wiring.

**The GUI itself is out of scope for this repo** — that's [#16](https://github.com/tguttzeit/5G-Measurement-Software/issues/16)
(backend) work. This repo only needs to define the contract: which keys/commands are remotely
addressable, the override-file mechanism, and the dispatch table.

## Reasoning

- Piggybacking on #4's heartbeat instead of a separate channel: zero marginal network cost (no
  additional radio wake-ups beyond what #4 already causes) and no inbound listening port ever, since
  the device always initiates contact — meaningfully safer than a tunnel and simpler to authenticate,
  since it reuses whatever auth the heartbeat already needs.
- Config vs. one-shot split: conflating them would either lose persistence for things that need it
  (a threshold change disappearing on the next boot defeats the point of remote tuning) or wrongly
  persist things that shouldn't be (re-running a SIM unlock isn't a standing config value).
- Allow-list over fully-generic remote config: a compromised or spoofed backend response is now a
  real risk given this carries executable commands, not just status — remotely rewriting
  `upload_host` could redirect where a day's data goes, and remotely rewriting a GPIO pin assignment
  could actively break the device. Restricting remote-write to data-quality/timing/behavior fields
  keeps the blast radius of a bad or malicious command bounded to "this run's data might be off,"
  never "the device is now unreachable or misdirected."
- "Prevent wasting a field day" as the inclusion principle (rather than listing config keys
  exhaustively up front): most of the specific config surface (exact fields for #3/#4/#7's
  thresholds) doesn't exist yet until those issues are actually implemented, so a principle that can
  be applied to future fields is more durable than an enumerated list decided today.