# Device bring-up FAQ

Notes from actually bringing a fresh device up and talking to a real backend for the first
time, over SSH with no screen attached - just the practical checklist/gotchas for doing this
again on another device.

## Tooling the Pi image doesn't ship with

A fresh device may have neither `uv` nor `gh` on `PATH`. Both are safe to install without a
reboot:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # then: source $HOME/.local/bin/env
sudo apt-get install -y gh                         # only needed if you're filing issues from the device itself
```

Then `uv sync` before anything else.

## config.toml has placeholders that must become real values per device

Several fields in the checked-in `config.toml` are deliberately obvious placeholders
(`"https://add.your.backend.here"`, `"/add/your/quectel-CM/path/here"`) - a fresh checkout
should never accidentally talk to a real backend or try to run a binary that isn't there. These
fields are this device's own identity/secrets, never generic defaults, so they don't belong in
the tracked `config.toml` at all - see "Keeping device-specific values out of git" below for
where they actually go:

- `[backend] url` - the backend host.
- `[backend] device_key`, `[heartbeat] hmac_secret`, `[heartbeat] device_id` - provisioned
  **backend-side** first (ask whoever administers the backend); the device can't generate these
  itself. Until they're set, `heartbeat`/`uploader` will correctly reach the backend but get
  `401 Unauthorized` - that's expected, not a wiring problem.
- `[device] device_id` - **must be set to the same value as `[heartbeat] device_id`.** They're
  separate fields for a reason (`[heartbeat] device_id` authenticates the device to the backend;
  `[device] device_id` is what gets baked into every captured datapoint), but if `[device]
  device_id` is left unset it silently defaults to the Pi's own hostname instead - which the
  backend will then reject on upload with `403: Datapoint device_id <hostname> does not match
  authenticated device <heartbeat device_id>`, since the uploaded data claims to be from a
  different device than the one that authenticated. Confirmed live on a real device where the
  hostname (`5gmu`) and the provisioned `[heartbeat] device_id` (`5gmu_1`) didn't match.
- `[quectel_cm] binary` - see below, this only exists after a build step.
- `[selftest] results_url`, `[gps_fix_test] status_url`, `[latency_test] host` - also embed a real
  hostname (backend/test server), same reasoning as `[backend] url` above. All stay off/inert
  (empty by default) until set - `[heartbeat] enabled` and `[latency_test] enabled` themselves are
  ordinary, non-secret fleet-wide defaults and stay in the tracked `config.toml`.

## Keeping device-specific values out of git

The fields above are per-device identity/secrets, not generic defaults every device in the
fleet shares - hand-editing them into the tracked `config.toml` leaves it permanently dirty and
uncommittable, and blocks pulling in future tracked-default changes without manually diffing
around your own edits.

Instead, create `config.local.toml` next to `config.toml` (same directory) - it's gitignored and
merged over `config.toml` at startup, so `config.toml` itself never needs to change:

```toml
[backend]
url = "https://your-real-backend.example.org"
device_key = "the real per-device API key"

[heartbeat]
device_id = "the real device id"
hmac_secret = "the real hmac secret"

[device]
# Must match [heartbeat] device_id above - see the mismatch warning further up this doc.
device_id = "the real device id"

[quectel_cm]
binary = "/home/admin/5GM-Software/vendor/quectel-CM/quectel-CM"

[selftest]
results_url = "https://your-real-backend.example.org/api/testing/results"

[gps_fix_test]
# Path includes this device's own id (see docs/device-contract.md on the backend) - substitute
# it by hand here, same as the id already set above under [heartbeat] device_id.
status_url = "https://your-real-backend.example.org/api/devices/your-device-id/gps-fix-test/status"

[latency_test]
host = "your-real-test-server.example.org"
```

Only these fields (and sections) are read from `config.local.toml` - anything else in the file is
logged and ignored. Give the file permissions readable only by the runtime user, e.g.
`chmod 600 config.local.toml`, since it holds plaintext secrets.

This replaces the older `git update-index --skip-worktree config.toml` workaround, which hid
`config.toml`'s dirty state but also silently stopped picking up upstream changes to it until
manually unset, pulled, and reapplied. If a device was set up with `--skip-worktree` before this,
undo it once (`git update-index --no-skip-worktree config.toml`), move the local values from
`config.toml` into a new `config.local.toml`, and let `config.toml` go back to tracking upstream.

On a real device (`system.running_on_pi = true`), startup logs a warning for any of these fields
still left unset or at its `config.toml` placeholder - a missing `config.local.toml` (or one that
leaves a field out) is loud in the log rather than a silent surprise later. It's a warning, not a
hard failure, since these fields already degrade gracefully at runtime (failed uploads, `401`s,
remote commands discarded) rather than crashing.

## Building quectel-CM

`vendor/quectel-CM` ships as source, not a binary:

```bash
cd vendor/quectel-CM && make release
```

Needs `gcc`/`make`/`pkg-config` (present on a stock Pi OS image) but not `autoconf`/`automake` -
the plain `Makefile` doesn't need them. Point `[quectel_cm] binary` at the resulting
`vendor/quectel-CM/quectel-CM`.

## quectel-CM needs an APN when the modem is in MBIM mode

If `quectel-CM`'s log says `Modem works in MBIM mode` / `must specify APN with '-s'`, it will sit
there without ever bringing `wwan0` up unless started with `-s <apn>`. Don't guess or look this
up externally - the SIM's own carrier already told the modem what to use. Query it directly over
the AT port:

```
AT+COPS?       # current carrier, e.g. +COPS: 1,0,"vodafone.de",13
AT+CGDCONT?    # the modem's own provisioned PDP contexts - context 1's APN is the one to use
```

This is faster and more reliable than looking up a generic public APN list, since it's reading
the exact value the SIM/carrier already negotiated for this specific device.

## RPi.GPIO isn't in the uv venv even on a real Pi

Setting `system.running_on_pi = true` makes `main.py` call into `core/system.py`'s GPIO
functions, which `import RPi.GPIO` inside the function body. That import fails with
`ModuleNotFoundError: No module named 'RPi'` even on real Pi hardware, because `RPi.GPIO`/
`rpi-lgpio` is deliberately not a `pyproject.toml` dependency (it doesn't exist off-Pi, so
`uv sync` must stay clean on dev machines), while the apt-installed `python3-rpi-lgpio` package
only puts the module on the *system* Python's path, not the project's `uv`-managed `.venv`.

On current Pi OS (Trixie+), classic `RPi.GPIO` doesn't work at all - GPIO goes through
`lgpio`/`libgpiod` instead, which is why apt installs `python3-rpi-lgpio` (a shim exposing the
`RPi.GPIO` API backed by `lgpio`) rather than `python3-rpi.gpio`. `uv pip install rpi-lgpio` tries
to build its `lgpio` C-extension dependency from source and needs `swig` plus `python3-dev`
headers - avoid pulling in a build toolchain for this. Since the venv and system Python are the
same version/arch, just symlink the already-built apt package into the venv instead:

```bash
SITE=.venv/lib/python3.13/site-packages   # match your venv's actual Python version
ln -sf /usr/lib/python3/dist-packages/RPi "$SITE/RPi"
ln -sf /usr/lib/python3/dist-packages/lgpio.py "$SITE/lgpio.py"
ln -sf /usr/lib/python3/dist-packages/_lgpio.cpython-*-linux-gnu.so "$SITE/"
```

Verify with `.venv/bin/python -c "import RPi.GPIO"` before restarting the service. This has to be
redone if the venv is ever recreated (`uv sync` alone won't touch it, but a deleted/rebuilt
`.venv` will lose the symlinks).

## Wiring the SSD1306 OLED display (I2C)

`[display]` connects over I2C - SDA/SCL are
fixed to GPIO2/GPIO3, not configurable, since those are the Pi's hardware I2C pins. On this
device's screen, the 4 pins run left to right as GND, VCC, SCL, SDA, wired with these cable
colors:

| Screen pin | Cable color | Pi physical pin | Pi GPIO |
|---|---|---|---|
| GND | white | 6 | Ground |
| VCC | violet | 1 | 3.3V |
| SCL | blue | 5 | GPIO3 (SCL) |
| SDA | green | 3 | GPIO2 (SDA) |

Matches `config.toml`'s `[display] i2c_port = 1`, `i2c_address = 0x3C`. The pin order/colors are
specific to this device's cable set, not a hardware standard - re-check them against the actual
screen's pin labels if wiring a different device rather than assuming this table.

## Testing hardware/backend wiring before a full run

`python -m measurement_software.selftest` (optionally naming specific checks, e.g.
`selftest modem uploader`) exercises each subsystem independently and reports pass/fail/skip -
much faster to iterate on than running `main.py` end-to-end while chasing one broken check.

A few things about it that are easy to misread as bugs the first time:

- The `uploader` check (and the real `Uploader` at runtime) only ever considers `wlan0`/`wwan0`
  for its "is there a network" gate - being reachable over `eth0` (e.g. the SSH session you're
  running commands from) does not satisfy it. If you're testing from a wired connection, this
  check will correctly fail with "No network interface (wlan0/wwan0) has an IPv4 address" even
  though the device clearly has internet.
- `gps`/`collector` will fail if no GNSS receiver is physically wired to `/dev/serial0` - that's
  the check doing its job, not a software problem, if the receiver genuinely isn't connected yet.
- The `modem` check can pass with **zero** cell samples even when the modem is fine, if its radio
  hasn't camped on a network yet - e.g. `quectel-CM` hasn't been started, so there's no active
  data session. Check whether the radio actually has a connection first
  (`AT+QENG="servingcell"` over the AT port, or `ip addr show wwan0`) before assuming a software
  problem.
