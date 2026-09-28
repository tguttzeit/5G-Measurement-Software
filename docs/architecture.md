# Architecture

- **Entry point** (`main.py`): loads `config.toml` into one `AppConfig` tree, builds the `Modem`
  and `GNSSReceiver`, recovers any unfinalized data from an interrupted prior run, then drives the
  run through its lifecycle. A remote `shutdown-now` command or a failed SIM unlock routes into a
  single unified shutdown path (`core/system.py::shutdown()`) that finalizes/uploads pending data,
  logs why, and powers down. A remote `reboot-now` command routes into the analogous
  `core/system.py::reboot()` instead, which never touches `system.shutdown_gpio` since the board
  stays powered throughout. An unhandled exception reaching the top level is escalated by
  `core/crash_policy.py::CrashPolicy`, whose state survives reboots: first a reboot; once the same
  error has occurred `system.crash_same_error_limit` times in a row (or after
  `system.crash_reboot_limit` reboots in a row), a next-boot `system_mode = "waiting"` override
  plus a reboot; and if even then an error reaches the top level, a reboot into maintenance mode
  (`core/maintenance_mode.py`). Maintenance mode is checked at the very start of `main()` and runs
  nothing but `quectel-CM` and the link watchdog, after one heartbeat with the `critical_error`
  run phase and a `critical_error` object, so the device stays reachable over SSH for debugging;
  it is left by deleting `crash_state.json` next to `config.toml` and rebooting. Every crash also
  leaves `last_crash_report.json` (fingerprint, counters, full traceback), which the next boot
  logs. `SIGTERM` (`systemctl stop`, a system shutdown) ends the run without any of this: the run
  log is finalized and uploaded best-effort, and nothing is counted as a crash. A remote
  `stop-measuring-now` command ends the run like an ordinary idle timeout (no unified
  shutdown/reboot path), after persisting a next-boot `system_mode = "waiting"` override. Both `shutdown()`
  and `reboot()` send one last best-effort heartbeat first, reporting the `shutting_down`/
  `rebooting` run phase and, if a remote command triggered the call, acking that command's id as
  `"received"` - the only ack either can ever send, since there is no later heartbeat tick to
  report a final success/failure from.
- **Config-driven, one `AppConfig` tree** (`core/config.py`): nested dataclasses built from
  `config.toml`; hardware, backend, and uploader sections are required, most others fall back to
  defaults if omitted. See [`configuration.md`](configuration.md) for every field. The device's
  `mission_type` selects a `[profiles.*]` table of override-field values (e.g. `drone`: no movement
  gate, no idle end) applied between `config.toml` and `config.local.toml`; components stay
  mission-agnostic and never branch on `mission_type`.
- **Hardware abstraction via ABCs + factories**: `Modem` and `GNSSReceiver` define `open`/`close`/
  query contracts; concrete implementations (`Quectel`, `NMEASerial`) are selected at runtime by a
  `config.type` string through small factory-dict modules. The same pattern is used for `Display`.
  See [`hardware-integration.md`](hardware-integration.md) to add a new implementation.
- **Lifecycle state machine** (`core/run_phase.py`, `core/movement_gate.py`): each boot cycle moves
  through a small named-state vocabulary (`RunPhaseState`) ending in `ACTIVE_MEASURING`. In **Auto
  Mode** (`config.system.mode = "auto"`, the default), `quectel-CM` and the heartbeat both start
  immediately at boot — the same way Waiting Mode already did — so the device is reachable for the
  whole run, not just once measuring starts. `MovementGate` then waits (`WAITING_FOR_GPS_FIX`, then
  `WAITING_FOR_MOVEMENT` once a fix exists) until displacement is confirmed across consecutive polls
  (and, if a home position is configured, departure from it), opportunistically syncing the system
  clock from GNSS-derived UTC time along the way, before the run transitions to `ACTIVE_MEASURING`.
  With `movement_gate.enabled = false` this wait is skipped and the run goes straight to
  `ACTIVE_MEASURING`.
  This trades away the original design's battery-efficiency (not opening the modem while waiting)
  for always-on visibility. Setting `config.system.mode` to
  `"waiting"` instead selects **Waiting Mode**: same immediate `quectel-CM`/heartbeat start, but it
  then blocks (`core/run_phase.py::WaitingLoop`, phase `WAITING_MODE_IDLE`) until an explicit
  remote command arrives: `start-measuring-now` transitions it into the same `ACTIVE_MEASURING`
  flow, while `run-selftest-now` runs the same hardware-reachability checks as the CLI selftest path
  (excluding side-effecting ones) without leaving Waiting Mode — for field diagnostics where the
  device needs to be remotely reachable before it starts moving, with no automatic movement-based
  transition. Mode switches (`mode-waiting-now`/`mode-auto-now`) only take effect on the next boot.
- **Collection loop** (`core/collector.py::Collector`): once measuring, waits for GPS fixes and
  captures a `Datapoint` (cell sample + GNSS fix + timestamp + run id) each time the vehicle clears a
  movement threshold since the last capture. Ends after sustained idle time. A background UDP
  heartbeat thread (`core/system.py::KeepModemAliveSender`) keeps the modem's link alive throughout.
- **Power-loss durability** (`core/run_log.py`, `core/util.py`): datapoints are appended one
  JSON line at a time to a per-run log and fsynced, so an interrupted run can lose at most its last
  incomplete line. Finalizing converts that log into the array-format file the uploader ships,
  written atomically; unfinalized logs and temp files are cleaned up or recovered at the next
  startup.
- **Run identifier** (`core/collector.py::Collector.run_id`, `core/util.py::new_run_id`): each
  run gets a random UUID4 string, shared with the latency tester, and every `Datapoint` and
  `LatencyResult` carries it as `run_id`. It is written into each log line, so a run recovered
  after a power cut keeps the id of the run that produced it. The backend treats the field as
  optional (uploads without it are assigned a legacy run), and it is not part of the heartbeat.
- **Uploader** (`core/uploader.py`): ships pending files via authenticated HTTPS POST to the
  configured backend (`X-Device-Key` header), only when `wlan0` or `wwan0` has an IPv4 address.
  Upload attempts fire at the waiting-phase-to-`ACTIVE_MEASURING` transitions rather than fixed
  points in the run. A file is deleted locally only after a successful upload.
- **Backend heartbeat and remote commands** (`remote/heartbeat.py`): a periodic heartbeat reports
  run health to the backend — including the current `run_phase` and GPS fix status
  (`status/backend_status.py`) — and its response can carry HMAC-signed, allow-listed config
  overrides (persisted to a local, gitignored `config_overrides.toml`) and one-shot commands
  (e.g. `force-upload-now`, `shutdown-now`), verified and dispatched by `RemoteCommandDispatcher`.
  Each heartbeat request carries a fresh random `nonce` that the backend must echo inside the signed
  response body; a response with overrides or commands and no matching nonce is discarded, so a
  captured response can't be replayed. The device mode (`system_mode`) is one of the override fields,
  and is also reported in `current_config`, as is every other override field.
  `promote-overrides-now` (or the standalone `python -m measurement_software.promote_overrides`)
  bakes whatever is currently set in `config_overrides.toml` into `config.toml` as the new
  baseline, via the same `promote_overrides()` function either path calls
  (`core/config_sources.py`), comment-preserving since it edits `config.toml` with `tomlkit`.
  Every dispatched command's outcome (`success`/`failed`, with the error if it failed) rides
  back to the backend as `executed_commands` on the next heartbeat - re-queued on every
  redelivery until the backend stops sending that command id, so a lost ack just gets retried
  on the usual redelivery schedule rather than needing its own retry logic.
- **Raspberry Pi glue** (`core/system.py`): GPIO setup/signaling, `quectel-CM` process management,
  GNSS-derived clock sync, and the unified shutdown sequence. `core/link_watchdog.py` restarts
  `quectel-CM` when `wwan0` stays without an IPv4 address, and after repeated restarts asks the
  `Collector` to reset the modem's radio (`AT+CFUN`) - only while measuring, since only then is
  the modem's serial port open on the collection thread. GPIO functions import `RPi.GPIO`
  inside the function body (only available on real Pi hardware) and are only ever called behind
  `config.system.running_on_pi`.
- **Manual hardware selftest** (`selftest.py`, `selftests/`): a separate
  `python -m measurement_software.selftest` entry point that exercises each hardware-facing
  subsystem against real hardware (one AT query, one NMEA read, etc.) and reports pass/fail — for
  diagnosing wiring problems on a specific Pi over SSH, distinct from the unit test suite.

## Testing patterns

The whole domain is I/O at the edges (serial ports, GPIO, subprocess, HTTP, wall-clock timing,
threads), so tests fake the boundary rather than mocking loosely: `Modem`/`GNSSReceiver` fakes
implement the real ABCs, `serial.Serial` and `subprocess`/`urllib.request` calls are patched at
their point of use with fakes that record what was sent, `RPi.GPIO` is injected via `sys.modules`
since it doesn't exist off-Pi, and time-dependent logic (`Collector`, `MovementGate`) patches the
clock instead of racing real wall-clock delays. Tests mirror the `src/measurement_software`
package layout under `tests/`.

## Code style

Readable code is preferred over comments explaining what it does. Comments are fine when a
concept genuinely can't be made obvious through naming/structure alone, but shouldn't substitute
for that effort.
