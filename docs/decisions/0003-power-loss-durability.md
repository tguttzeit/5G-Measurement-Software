# 0003 — Power-loss durability: JSONL during collection, convert at finalization

## Context

[Issue #9](https://github.com/tguttzeit/5G-Measurement-Software/issues/9) — today, `Collector.collect()`
holds every `Datapoint` in memory for the whole run and `Uploader.save_datapoints()` writes them
once, non-atomically, at the very end. A power cut at any point during a run loses everything;
a power cut during that final write can corrupt even the portion that made it that far.

## Decision

**Durability format during collection**: append each capture event's `Datapoint`s as JSON Lines
(one JSON object per line) to a per-run `.jsonl` file, with `fsync()` after every append.

**Finalization**: a single shared step — read the `.jsonl` file, skip/log any unparseable trailing
line (a partial write from a crash mid-append), convert the valid lines into the existing
JSON-array format, and write that atomically (temp file in the same directory + `os.replace()`).
The `.jsonl` file is only deleted once the converted array file is confirmed written. This step is
used identically in two places: finalizing the current run's data once `collect()` ends normally,
and recovering a leftover `.jsonl` found at startup from a run that crashed before finishing.

**Recovery policy**: always attempt to finalize/recover a leftover `.jsonl` at startup, regardless
of whether it's from today's mission or an earlier one. What happens to that recovered data
afterward (upload timing/ordering relative to the current run) is out of scope here — split off to
[#18](https://github.com/tguttzeit/5G-Measurement-Software/issues/18).

**Upload format unchanged**: the uploaded/backend-facing format stays the existing JSON array — this
decision only changes what happens locally during collection, not the wire format.

## Reasoning

- JSONL (not the current single JSON array) was chosen specifically for its partial-write
  resilience: a truncated append corrupts only the last, incomplete line — every prior line is
  still independently valid JSON. A single JSON array file doesn't have this property; one
  truncated byte anywhere makes the *entire* file unparseable, regardless of how much of the run it
  represents.
- Considered switching the uploaded format itself to JSONL too, to skip the conversion step
  entirely — rejected in favor of converting back to the existing array format at finalization, so
  this stays a purely local durability fix with zero schema impact on the backend
  ([#16](https://github.com/tguttzeit/5G-Measurement-Software/issues/16), not built yet).
- `fsync()` per append was chosen over relying on OS page-cache flushing, since page-cache-buffered
  writes are exactly the data still vulnerable to an unclean power cut — the scenario this issue
  exists for. The battery/wear cost of this was raised and is believed (not verified) to be
  negligible relative to the modem/GNSS radio costs already dominating this system's power budget;
  an actual bench measurement would be needed for certainty if this ever becomes a concern.
- Durability granularity (per capture event, local disk only) was deliberately kept independent of
  upload granularity/timing (`Uploader.upload_pending_files()`, unchanged by this decision) — local
  SD writes don't touch the cellular radio, so there's no battery trade-off to make here the way
  there was for the heartbeat design in
  [0001](0001-extended-heartbeat-payload.md)/[0002](0002-heartbeat-quality-thresholds.md).