# 0008 — Unified shutdown path

## Context

[Issue #10](https://github.com/tguttzeit/5G-Measurement-Software/issues/10) — originally just "the
except block doesn't shut the Pi down." By the time this was revisited, three other issues had each
grown their own reason to trigger a shutdown: #7's planned day-end, #8's remote shutdown-now command,
and #5's SIM-unlock hard failure (already folded into this issue). This record settles that these
should be one mechanism, not four independent ones.

## Decision

**One shutdown path, parameterized by reason, not four separate call sites.** A single function
handles the actual shutdown sequence; every trigger point calls it with a reason rather than
implementing its own version:

1. Unhandled exception reaching `main()`'s top level (this issue's original bug)
2. Planned day-end — [#7](https://github.com/tguttzeit/5G-Measurement-Software/issues/7)'s
   time-of-day cutoff while in `LOW_POWER_WAITING`
3. Remote shutdown-now command — [#8](https://github.com/tguttzeit/5G-Measurement-Software/issues/8)
4. SIM-unlock hard failure — [#5](https://github.com/tguttzeit/5G-Measurement-Software/issues/5)

**The sequence is identical regardless of reason** (confirmed with the maintainer — no
reason-dependent branching in the shutdown behavior itself):

1. Finalize whatever data exists — with [#9](https://github.com/tguttzeit/5G-Measurement-Software/issues/9)'s
   redesign this mostly reduces to converting the current in-progress durability chunk, since almost
   everything is already persisted continuously by that point.
2. Attempt upload if network's available (existing `Uploader` behavior, unchanged).
3. Log the reason (plain logging — confirmed not to need a dedicated final-heartbeat message for
   this; #4/#8's channel could carry it later if that turns out useful, but it's out of scope now).
4. Power down the modem, GPIO-signal completion, actually shut down — today's `perform_shutdown()`/
   `cleanup_gpio()`, re-evaluated rather than assumed correct, per this issue's existing note that
   `perform_shutdown()` was carried over as-is from the prototype and never rethought.

**Top-level exception handling rule**: anything that propagates all the way up to `main()`'s top
level is treated as unconditionally fatal — no "should this retry instead" branching happens at that
level. Retry logic belongs in the specific subsystems that know whether something is safely
retriable — [#12](https://github.com/tguttzeit/5G-Measurement-Software/issues/12) (transient serial
failures) and [#5](https://github.com/tguttzeit/5G-Measurement-Software/issues/5) (SIM PIN
transient-vs-rejected). If it reaches the top, it's fatal, full stop.

## Reasoning

- One shutdown path instead of four: the four trigger points converged on wanting the same sequence
  independently, once #7/#8/#5 were designed — writing four separate implementations would mean
  four places that could silently drift out of sync (e.g. one path forgetting to attempt upload
  before powering down).
- Uniform sequence regardless of reason: simpler to build and test than reason-dependent branching,
  and the maintainer confirmed there's no actual behavioral difference needed — only the logged
  reason differs.
- Pushing retry decisions down to specific subsystems rather than deciding "is this exception
  retriable" at the top level: `main()`'s top level doesn't have the context to know whether a given
  failure is transient — that context lives with whatever raised it. Centralizing the retry decision
  there would mean either an ever-growing type-based dispatch of "which exceptions are retriable," or
  under- or over-retrying blindly. Cleaner to make the rule "if it got this far, it's fatal" and let
  #12/#5 handle their own retry logic before ever letting an exception escape that far.