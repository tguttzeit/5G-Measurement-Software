# 5G Measurement Software

A Raspberry Pi field-logging tool for 5G/LTE measurement campaigns. It correlates cellular
serving-cell measurements (via a Quectel modem's AT command interface) with GPS position (via a
serial NMEA GNSS receiver), then uploads the results to a remote backend over HTTPS. It runs
unattended on a Pi: on boot it brings up the cellular link and starts reporting to the backend,
waits for the vehicle to actually start moving, collects data for the day, uploads what it has,
and shuts itself down when finished.

The documentation (architecture, every `config.toml` field, wiring up different modem/GNSS/display
hardware, device setup, local testing) lives on the documentation site:
[English](https://bosdra.net.tha.de/docs/en/), [Deutsch](https://bosdra.net.tha.de/docs/de/).

## Prerequisites

Build `quectel-CM`, Quectel's bundled connection-manager daemon that establishes the actual
cellular link (this repo only talks to the modem over AT commands to *read* measurements, it
doesn't manage the connection itself):

```bash
cd vendor/quectel-CM
make release
```

`release` is the default target and cleans first. Point `[quectel_cm] binary` at the resulting
`vendor/quectel-CM/quectel-CM`.

For the optional latency tests (`[latency_test]` in `config.toml`), install flent and the tools
it drives on the device:

```bash
sudo apt install flent netperf python3-numpy
```

The tests also need a counterpart on the test server: `netperf`'s `netserver` for the load test,
and a host that answers pings for the baseline. Leave `[latency_test] host` unset until that side
is reachable from the device: without a host no tests run, so an unconfigured device never
generates test traffic.

## Setup

```bash
uv sync
```

Then edit `config.toml` at the repo root (it's read by path relative to the installed package, not
from the current working directory) — at minimum, set `[gnss_receiver] port` for your hardware,
`[backend] url`/`device_key`, and `[uploader] upload_dir`. `[modem] port` can stay unset: the
modem's AT port is then found automatically. Every section is commented with what it controls and
why the shipped defaults are what they are.

## Running

```bash
uv run python -m measurement_software.main
```

This runs one full cycle: recover any unfinalized data from a previous run, start `quectel-CM`
and the backend heartbeat, wait for confirmed movement, collect measurements while moving,
finalize and upload, then (if `[system] running_on_pi = true`) shut the Pi down. There's no
`[project.scripts]` entry point — always invoke it as a module.

To manually verify real hardware wiring on a Pi over SSH (modem, GNSS, uploader, etc., without
running a full measurement cycle), see `python -m measurement_software.selftest --help`.

## Autostart on boot

`systemd/5g-measurement.service` runs `python -m measurement_software.main` on boot. It does not
require or start `quectel-CM` — the app starts it itself, right at boot, so the device is
reachable over the cellular link for the whole run and not only once measuring begins. It also
stops `quectel-CM` again: on `systemctl stop` or a system shutdown, systemd signals only the app
(`KillMode=mixed`), so its last heartbeat and upload still go out over the cellular link.

To install it, run this from the checkout; the unit file contains an `@INSTALL_DIR@` placeholder
that is replaced by the current directory. Run it again after the unit file changed:

```bash
sed "s|@INSTALL_DIR@|$PWD|g" systemd/5g-measurement.service \
  | sudo tee /etc/systemd/system/5g-measurement.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable 5g-measurement.service
```

## Testing

```bash
uv run pytest                 # full suite
uv run pytest --cov=measurement_software --cov-report=term-missing   # coverage
uv run ruff check .           # lint
```

See the [documentation site](https://bosdra.net.tha.de/docs/en/) for the project's testing patterns.
