# 5G Measurement Software

A Raspberry Pi field-logging tool for 5G/LTE measurement campaigns. It correlates cellular
serving-cell measurements (via a Quectel modem's AT command interface) with GPS position (via a
serial NMEA GNSS receiver), then uploads the results to a remote backend over HTTPS. It runs
unattended on a Pi: on boot it waits battery-efficiently for the vehicle to actually start moving,
collects data for the day, uploads what it has, and shuts itself down when finished.

See [`docs/architecture.md`](docs/architecture.md) for how it's put together,
[`docs/configuration.md`](docs/configuration.md) for what every `config.toml` field does, and
[`docs/hardware-integration.md`](docs/hardware-integration.md) for wiring up different modem/GNSS/
display hardware.

## Prerequisites

Build `quectel-CM`, Quectel's bundled connection-manager daemon that establishes the actual
cellular link (this repo only talks to the modem over AT commands to *read* measurements, it
doesn't manage the connection itself):

```bash
cd vendor/quectel-CM
make clean
make
```

For the optional latency tests (`[latency_test]` in `config.toml`), install flent and the tools
it drives on the device:

```bash
sudo apt install flent netperf python3-numpy
```

The tests also need a counterpart on the test server: `netperf`'s `netserver` for the load test,
and a host that answers pings for the baseline. Until that side is confirmed, leave
`[latency_test] enabled = false`.

## Setup

```bash
uv sync
```

Then edit `config.toml` at the repo root (it's read by path relative to the installed package, not
from the current working directory) — at minimum, set `[modem] port`/`[gnss_receiver] port` for
your hardware, `[backend] url`/`device_key`, and `[uploader] upload_dir`. Every section is
commented with what it controls and why the shipped defaults are what they are.

## Running

```bash
uv run python -m measurement_software.main
```

This runs one full cycle: recover any unfinalized data from a previous run, wait for confirmed
movement, collect measurements while moving, finalize and upload, then (if
`[system] running_on_pi = true`) shut the Pi down. There's no `[project.scripts]` entry point —
always invoke it as a module.

To manually verify real hardware wiring on a Pi over SSH (modem, GNSS, uploader, etc., without
running a full measurement cycle), see `python -m measurement_software.selftest --help`.

## Autostart on boot

`systemd/5g-measurement.service` runs `python -m measurement_software.main` on boot. It does not
require or start `quectel-CM` — the app waits, battery-efficiently, for confirmed vehicle
movement before starting `quectel-CM` and opening the modem itself. To install it:

```bash
sudo cp systemd/5g-measurement.service /etc/systemd/system/
sudo systemctl enable 5g-measurement.service
```

## Testing

```bash
uv run pytest                 # full suite
uv run pytest --cov=measurement_software --cov-report=term-missing   # coverage
uv run ruff check .           # lint
```

See [`docs/architecture.md`](docs/architecture.md) for the project's testing patterns.
