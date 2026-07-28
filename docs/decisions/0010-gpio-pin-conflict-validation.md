# 0010 — GPIO pin conflict validation scope

## Context

[Issue #14](https://github.com/tguttzeit/5G-Measurement-Software/issues/14) — coordinating GPIO pin
assignments across the shutdown-signal, display ([#2](https://github.com/tguttzeit/5G-Measurement-Software/issues/2)),
and fan control ([#6](https://github.com/tguttzeit/5G-Measurement-Software/issues/6)) features. This
record settles the validation's actual scope after an initial miscount was caught and corrected.

## Decision

**A lightweight validation function, not a general pin-reservation config system.** At config-load
time, gather every pin currently in use into one set and check for overlaps — not a `[gpio]`-style
registry section, since only a small, known set of consumers exists today.

**The set includes both configurable and fixed entries**:
- Configurable: `system.shutdown_gpio`, whatever pin field(s) [#6](https://github.com/tguttzeit/5G-Measurement-Software/issues/6)'s
  fan control defines (one pin for on/off, possibly a second for PWM).
- **Fixed, hardcoded**: GPIO2/GPIO3 (SDA1/SCL1), added to the set whenever `[display]` is enabled —
  not something read from config, since I2C's pins are physically fixed on the Pi, not assignable.

Any overlap anywhere in that combined set raises a clear config-load-time error.

## Reasoning

- Initially scoped this down to *only* compare the two freely-configurable values
  (`shutdown_gpio` vs. fan pin), reasoning that I2C is "a separate bus, not a GPIO-number conflict."
  That's true for *display-vs-display* or *I2C-device-vs-I2C-device* conflicts, but wrong for the
  actual risk this issue is about: someone configuring `shutdown_gpio` or a fan pin to `2` or `3`
  without realizing those physical pins are already claimed by I2C once the display is enabled.
  Dropping the display from the conflict set entirely was the mistake — the fix is to include its
  fixed pins as reserved entries, not to exclude it.
- Still not a general reservation *system*: with only three consumers total (shutdown signal, fan,
  display) and two of them sharing one lightweight validation function, building an extensible
  `[gpio]` config section for hypothetical future consumers would be solving for a problem that
  doesn't exist yet — consistent with the scoping-down reasoning in
  [0009](0009-storage-growth-scope.md) for #13. A fourth GPIO-consuming feature, if one ever appears,
  can be added to the same set-and-check function cheaply.