# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Raspberry Pi field-logging tool for 5G/LTE measurement campaigns. It correlates cellular
serving-cell measurements (via a Quectel modem's AT command interface) with GPS position (via a
serial NMEA GNSS receiver), then uploads the results to a remote server over scp. It runs
unattended on a Pi: on boot it collects data while the device is moving, uploads what it has, and
(if configured) shuts the Pi down when finished.

`vendor/quectel-CM` is Quectel's bundled C connection-manager daemon (build separately per its own
README) — it establishes the actual cellular network link (PPP/QMI). The Python code in this repo
does not manage that connection; it only talks to the modem over its AT serial port to *read*
measurements.

## Prototype origins

Everything in this repository up to and including commit `12e5f25` ("Add CLAUDE.md") — except the
unit-test commit (`396f4b9`, "Add unit tests for every necessary file") and this file itself — is a
refactored version of an original proof-of-concept prototype. Confirmed with the maintainer: none
of it should be read as a finished, final implementation, either in coding quality or in the
design/architectural thinking behind it. Treat existing patterns here as a starting point to
question and improve, not as an established convention to preserve for its own sake.

## Commands

```bash
uv sync                      # install dependencies (also used as the devcontainer postCreateCommand)
uv run pytest                # run the full test suite
uv run pytest tests/core/test_collector.py::TestCollect::test_returns_empty_list_when_no_first_fix
                              # run a single test
uv run pytest --cov=measurement_software --cov-report=term-missing   # coverage
uv run ruff check .          # lint
uv run python -m measurement_software.main   # run the app (reads config.toml at repo root)
```

There is no `[project.scripts]` entry point; the app is only run via `python -m measurement_software.main`.

## Architecture

**Entry point**: `src/measurement_software/main.py::main()` — loads `config.toml` (repo root, found
via `Path(__file__)`, not cwd), sets up logging, builds a `Modem` and `GNSSReceiver` via factories,
recovers anything a previous run left unfinalized, runs one `Collector.collect()` cycle, finalizes
that run's log, uploads results via `Uploader`, and (if
`system.running_on_pi`) signals completion over GPIO. `if __name__ == "__main__"` then calls
`perform_shutdown()` to power down the modem and run `sudo shutdown -h now` — this only fires when
run as a script, not when `main()` is called directly (e.g. from tests).

**Config-driven, one `AppConfig` tree** (`core/config.py`): `load_config()` reads `config.toml` and
builds nested dataclasses. `modem`, `gnss_receiver`, `uploader` sections are required (`KeyError` if
missing); `collector`, `logging`, `system` are optional and fall back to dataclass defaults if the
TOML section is absent.

**Hardware abstraction via ABCs + factories**: `Modem` (`modems/modem.py`) and `GNSSReceiver`
(`gnss/gnss_receiver.py`) define `open`/`close`/query contracts. Concrete implementations are
selected at runtime by a `config.type` string through tiny factory-dict modules —
`modems/__init__.py::create_modem` and `gnss/__init__.py::create_gnss_receiver`. Currently the only
registered implementations are `Quectel` (`modems/quectel.py`) and `NMEASerial`
(`gnss/nmea_serial.py`), both keyed as `"quectel"` in their factory dicts.

> Note: `config.toml`'s `[gnss_receiver] type` is currently set to `"nmea_serial"`, but
> `gnss/__init__.py`'s factory dict only registers the key `"quectel"`. As written, loading that
> config would raise `ValueError` from `create_gnss_receiver`. Worth checking before relying on the
> checked-in `config.toml` as-is.

**Collection loop** (`core/collector.py::Collector`): waits for a first GPS fix, then on each
subsequent fix computes haversine distance from the last captured position; if it clears
`position_threshold`, queries the modem and pairs each `CellSample` with the `GNSSFix` + timestamp
as a `Datapoint` appended to the run log. The session ends once there's no qualifying movement for
`max_idle_time` seconds, or if no fix arrives at all within `max_wait_for_first_fix`; `collect()`
returns the path of the run log it wrote. A background UDP heartbeat
(`core/keep_modem_alive_sender.py::KeepModemAliveSender`, its own thread) runs for the duration of
collection to keep the modem's link alive.

**Power-loss durability** (`core/run_log.py`, `core/atomic_file.py`): per
[ADR 0003](docs/decisions/0003-power-loss-durability.md), datapoints are never held for a whole run
in memory. `RunLog` appends each capture as one JSON object per line to a per-run
`gps_5g_<timestamp>.jsonl` in `upload_dir` and fsyncs it, so an interrupted append can only ever
lose the last, incomplete line. `finalize()` converts that log into the unchanged `*.json` array
format the uploader ships, written atomically (temp file in the same directory + `os.replace()` +
fsync of file and directory), and removes the log only afterwards. `recover_unfinalized()` and
`discard_stale_temp_files()` run at startup to clean up after a run that was cut short; the `*.jsonl`
and `*.json.tmp` names deliberately don't match `upload_pending_files()`'s `*.json` glob, so
in-progress data can never be uploaded.

**Uploader** (`core/uploader.py`): `upload_pending_files()` scp's every pending `*.json` file to the
remote host, but only if `wlan0` or `wwan0` has an IPv4 address; a file is deleted locally only after a successful
transfer, so failures just leave it for the next run. `debug_upload` additionally binds the scp
connection to `wwan0`'s IP (for testing over the cellular link itself) and adds `-vv`.

**Raspberry Pi glue** (`core/system.py`): GPIO functions (`setup_gpio`, `signal_completion`,
`cleanup_gpio`) do `import RPi.GPIO` *inside* the function body, since that library only exists on
real Pi hardware, not in dev environments — this is why they're only ever called behind
`config.system.running_on_pi` checks in `main.py`. `perform_shutdown()` best-effort powers down the
modem (any failure is caught and logged, not raised) before shutting the system down regardless.

## Testing patterns

The whole domain is I/O at the edges (serial ports, GPIO, subprocess/scp, wall-clock timing,
threads), so every test fakes the boundary rather than mocking loosely:

- `Modem`/`GNSSReceiver` fakes implement the actual ABC (not a generic `MagicMock`), so tests
  exercise the real interface contract.
- `serial.Serial` is patched at the point of use (e.g.
  `measurement_software.modems.quectel.serial.Serial`) with a fake that records writes and returns
  scripted response bytes — no real serial port is ever touched.
- `Collector` tests patch `time.time`/`time.sleep` in `measurement_software.core.collector`'s
  namespace with a fake clock, and patch `KeepModemAliveSender` itself (it spawns a real thread and
  sends real UDP packets otherwise), so fix-waiting and idle-timeout logic run deterministically and
  instantly instead of racing the wall clock.
- `RPi.GPIO` doesn't exist on dev machines. Tests for `core/system.py`'s GPIO functions inject a
  fake module via `sys.modules["RPi"]` / `sys.modules["RPi.GPIO"]` before calling code that does the
  deferred `import RPi.GPIO`.
- `subprocess.run`/`subprocess.check_output` (scp, `ip addr show`, `sudo shutdown`) are patched
  per-module with fakes that record the exact command invoked and return a canned
  `CompletedProcess`/output string, so tests can assert on the actual command built (flags, remote
  path, port) without shelling out.

Tests mirror the `src/measurement_software` package layout under `tests/` (e.g.
`tests/core/test_collector.py`, `tests/modems/test_quectel.py`).

## GitHub issues

Issues in this repo are drafted by Claude Code. Any "Suggested approach" section in an issue is
Claude's own proposal, not an approach the maintainer has approved — it must be discussed and
agreed with the maintainer before implementation work starts on it.

## Decision records

Non-obvious design decisions worked out with the maintainer while scoping an issue (trade-offs
weighed, alternatives rejected and why) get written up in `docs/decisions/` — see that directory's
README for format. This is separate from the issue itself: the issue tracks what needs doing, the
decision record captures how/why a specific approach was chosen. Add one whenever a real trade-off
was discussed and resolved, not for every issue.

## Autonomous work on `claude-ready` issues

An issue labeled `claude-ready` has been explicitly approved by the maintainer to be picked up and
implemented autonomously (e.g. so it can be kicked off and checked on later from a phone). For such
an issue:

- Create a branch named `<issue-id>-<issue_title_with_underscores_instead_of_spaces>` (e.g. issue
  12, "Handle transient serial connector failures" → `12-handle_transient_serial_connector_failures`).
- Implement the issue on that branch with modular, logically-ordered commits — each commit should
  build on the previous one so the work can be retraced commit by commit.
- Commit size rule of thumb: if the commit message needs an "and", it should be at least two
  commits instead.
- Never merge the branch yourself. Leave it for the maintainer to review and merge.
- Prefer readable code over comments explaining what it does — see `core/collector.py::collect()`
  as the target style. Comments are fine when a concept genuinely can't be made obvious through
  naming/structure alone, but shouldn't be substituting for that effort.