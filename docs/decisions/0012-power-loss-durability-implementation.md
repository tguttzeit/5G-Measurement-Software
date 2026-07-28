# 0012 — Power-loss durability: the choices ADR 0003 left open

## Context

[Issue #9](https://github.com/tguttzeit/5G-Measurement-Software/issues/9), implemented in
[#24](https://github.com/tguttzeit/5G-Measurement-Software/pull/24).
[ADR 0003](0003-power-loss-durability.md) settled the shape of the durability design: JSONL with
`fsync()` during collection, a shared finalization step converting it to the existing JSON-array
format atomically, and recovery of leftover logs at startup.

Actually building that surfaced further decisions 0003 doesn't cover — mostly about what happens at
the edges of "interrupted", where the answer is only obvious once you enumerate where a power cut
can actually land. This record captures those, so the reasoning is retraceable alongside 0003
rather than living only in the pull request.

## Decision

**1. In-progress and half-written files are kept out of the uploader's reach by naming, not by
filtering.** `upload_pending_files()` globs `*.json`; the run log is `*.jsonl` and the atomic
write's temp file is `*.json.tmp`, neither of which that glob matches. No skip-list, no marker
files, no upload-side logic at all.

**2. Finalization is idempotent, so an interruption between its rename and its cleanup is harmless.**
`finalize()` writes the array file, then removes the run log. A cut in between leaves both on disk;
the next startup finds the log, converts it again, and overwrites the array file with identical
content.

**3. A run log holding nothing usable is discarded rather than finalized into an empty `[]` file.**
This covers a run that never got a GPS fix and a log whose only line was cut off mid-write.

**4. A run's filename records when the run started, not when its data was saved.** The previous
`save_datapoints()` timestamped at write time, i.e. run end; the run log has to exist before the
first capture, so the same `gps_5g_<timestamp>` name now marks run start.

**5. Every append is fsynced, and so is the directory entry when the log is first created.** An
append with no datapoints in it costs no fsync.

**6. `collect()` returns the run log's path instead of a list of datapoints, and
`Uploader.save_datapoints()` is deleted rather than kept.** The uploader is left with one job —
moving finished files to the server — and there is exactly one piece of code that writes measurement
data to disk.

Minor, for the record: `Datapoint` moved from `collector.py` into its own module, because the
collector and the durability layer both need it and leaving it in place would have made that a
circular import.

## Reasoning

- **On (1)** — the issue's last bullet worried that a half-written file "would currently either fail
  to upload or, worse, upload corrupted data silently". Making the unsafe states unrepresentable to
  the uploader is stronger than teaching the uploader to recognise and skip them: a skip-list is code
  that can be got wrong, or forgotten when a new intermediate file type appears, whereas a file that
  doesn't match the glob cannot be selected by any code path at all. The cost is that the naming
  convention is now load-bearing, which is why there are tests asserting the glob doesn't pick up
  either name — the constraint is enforced rather than just documented.
- **On (2)** — the alternative was to detect the leftover pair and skip re-converting, which needs a
  way to decide whether the existing array file is complete. That check is exactly the kind of thing
  that is hard to get right and pointless to attempt: re-running the conversion is cheap, and since
  it derives the array file purely from the log, redoing it necessarily produces the same bytes.
  Idempotence by construction beats a correctness check.
- **On (3)** — an empty array file would be uploaded, and would be indistinguishable at the backend
  from a run that genuinely produced nothing measurable. Discarding it keeps "a file arrived" meaning
  "there is data in it". Considered and rejected: keeping the empty log as evidence a run happened —
  that's telemetry, and belongs on the heartbeat channel
  ([0001](0001-extended-heartbeat-payload.md)), not in the upload queue.
- **On (4)** — a real consequence worth being aware of when reading the data: a file named for
  09:00 may contain measurements up to 11:00. Start-time naming was not chosen for its own sake, it
  is forced by writing incrementally. The alternative — renaming the file at finalization to an
  end-time name — was rejected as a second rename doing nothing but preserving a cosmetic property,
  on the very path where fewer moving parts is the whole point. Note the recovery case has no end
  time to use anyway: nothing was running when the run ended.
- **On (5)** — this is 0003's `fsync()`-per-append decision followed through to the directory entry.
  A file whose creation is still only in the page cache can vanish entirely on a cut, taking data
  that was itself fsynced with it, so syncing the data without syncing the entry would leave a hole
  in exactly the scenario the fsyncs exist for. Skipping the fsync for an empty append is free: there
  is nothing to make durable, and captures that yield no cell samples aren't rare enough to ignore.
- **On (6)** — keeping `save_datapoints()` alongside `finalize()` would leave two ways to write
  measurement data to disk, one of them the non-atomic single `json.dump()` that this issue exists to
  remove. Nothing called it any more, but a still-reachable method is a live invitation to call it
  again later, and the bug would come back with it. Likewise, returning the datapoints from
  `collect()` as well as writing them would mean two representations of the same run, only one of
  which survives a power cut — the return value would quietly look like the authoritative one at the
  call site. Having the collector hand back the path to the record is the same information without
  the ambiguity. The cost is a wider diff (the collector's tests now assert against what was
  persisted rather than what was returned), which is arguably an improvement: they exercise the
  durability path end to end.
