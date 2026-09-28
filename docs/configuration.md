# Configuration reference

`config.toml` at the repo root is read by path relative to the installed package, not the current
working directory, and is loaded by `core/config.py::load_config()`. Sections marked **required**
raise `KeyError` if missing; everything else falls back to the dataclass defaults below if the
section (or field) is absent from `config.toml`.

Three files layer on top of `config.toml`, in this order: `config.toml` defaults, then
`config.local.toml` (per-device identity/secrets set by hand once at setup — see
[`device-bringup-faq.md`](device-bringup-faq.md)), then `config_overrides.toml` (remote runtime
tuning pushed by the backend). A field can only ever appear in one of the last two: identity/secret
fields are excluded from remote override entirely, enforced at import time in `config_local.py`.

## `[modem]` (required)

Serial connection and query-mode settings for the cellular modem.

| Field | Default | Meaning |
|---|---|---|
| `type` | — | Selects the `Modem` implementation via the factory in `modems/__init__.py`. Only `"quectel"` is registered today. |
| `port` | — | Serial device path, e.g. `/dev/ttyUSB2`. |
| `baud_rate` | — | Serial baud rate. |
| `timeout` | — | Serial read timeout in seconds. |
| `mode` | `"serving_cell"` | `"serving_cell"` (`AT+QENG`) or `"sa_scan"` (`AT+QSCAN`, NR5G-SA-only — requires manually setting band/mode via AT console first; see the comment in `config.toml`). |
| `retries` | `3` | Retry attempts for a failed AT query. |
| `retry_delay_s` | `0.75` | Delay between retries. |
| `sim_pin_env_var` | `"MODEM_SIM_PIN"` | Name of the environment variable holding the SIM PIN, read at unlock time — the PIN itself is never stored in a config file. |
| `sim_unlock_poll_attempts` | `10` | How many times to poll the SIM's lock status while waiting for an unlock to take effect. |
| `sim_unlock_poll_interval` | `1.0` | Delay between those polls, in seconds. |

## `[gnss_receiver]` (required)

Serial connection settings for the GNSS receiver.

| Field | Default | Meaning |
|---|---|---|
| `type` | — | Selects the `GNSSReceiver` implementation via `gnss/__init__.py`. Only `"nmea_serial"` is registered today. |
| `port` | — | Serial device path, e.g. `/dev/serial0`. |
| `baud_rate` | — | Serial baud rate. |
| `timeout` | — | Serial read timeout in seconds. |
| `retries` | `3` | Retry attempts for a failed read. |
| `retry_delay_s` | `0.75` | Delay between retries. |

## `[device]`

Identifies this device/vehicle and mission type, carried on every captured datapoint.

| Field | Default | Meaning |
|---|---|---|
| `device_id` | Pi's hostname | Per-device identifier baked into every datapoint. Already distinct per device in a typical fleet, so a single-device setup needs no configuration at all. Set explicitly only to override the hostname. |
| `mission_type` | `"ground"` | Free-form mission/campaign label. |

## `[collector]`

Thresholds controlling when a measurement is captured and when a session ends.

| Field | Default | Meaning |
|---|---|---|
| `position_threshold` | `15` | Minimum haversine distance (meters) from the last captured position before the next measurement is taken. |
| `max_idle_time` | `200` | Seconds without qualifying movement before the collection session ends. |
| `max_wait_for_first_fix` | `300` | Seconds to wait for an initial GPS fix before giving up on the run entirely. |
| `wait_log_interval` | `5` | How often (seconds) to log a "still waiting for fix" message. |
| `keep_alive_host` | `"8.8.8.8"` | Host the background UDP keep-alive sender targets, to keep the modem's link alive during collection. |
| `keep_alive_port` | `53` | Port for the same keep-alive traffic. |
| `keep_alive_interval_s` | `4.0` | Interval between keep-alive packets. |
| `gps_enabled` | `true` | When `false`, the collector runs without a real GPS fix (placeholder fixes), for testing without GNSS hardware attached. |
| `gps_disabled_poll_interval_s` | `5.0` | Poll interval used while `gps_enabled = false`. |

## `[movement_gate]`

Pre-flight wait for confirmed vehicle movement, before `Collector.collect()` starts. Runs
GNSS-only, at a low duty cycle — it never touches the modem or `quectel-CM` itself.

| Field | Default | Meaning |
|---|---|---|
| `poll_interval_s` | `120.0` | How often to poll GNSS while waiting for movement. |
| `movement_threshold` | `15.0` | Minimum distance (meters) between polls to count as movement. |
| `confirmations_required` | `2` | Consecutive movement-clearing polls required before a run is considered started. |
| `home_latitude` / `home_longitude` | unset | Optional known garage/home position. Both must be set together (validated at startup) or both left unset. When set, departure from this position is additionally required before a run starts, on top of the distance-based movement detection. |
| `home_departure_threshold` | `100.0` | Distance (meters) from the home position that counts as "departed". |

## `[quectel_cm]`

The `quectel-CM` connection-manager daemon, started by this app itself (via `sudo`) once movement
is confirmed — not auto-started by systemd at boot.

| Field | Default | Meaning |
|---|---|---|
| `binary` | placeholder path | Path to the built `quectel-CM` binary. Per-device — set the real path in `config.local.toml`, never here; the shipped placeholder is intentionally never a real path. |
| `args` | `()` | Extra arguments passed to `quectel-CM`, e.g. `["-s", "web.vodafone.de"]` for the APN. |

## `[run_status]`

What the heartbeat reports about run quality. A measurement counts as "good" only if it clears
every cutoff below for its RAT; falling short on any one makes it "bad". Cutoffs only apply to
readings that are physically possible in the first place — an impossible value counts as
"invalid" instead, decided from the radio standard rather than these thresholds.

| Field | Default | Meaning |
|---|---|---|
| `empty_captures_until_pipeline_broken` | `3` | Consecutive empty captures while moving before the heartbeat reports the measurement pipeline as broken. |

### `[run_status.lte]` / `[run_status.nr]`

Per-RAT quality cutoffs. Defaults are commonly-cited reference values (the kind drive-test tools
use as default tiers) and have not been validated against this project's own measurements — retune
per RAT once field data exists.

| Field | Default | Meaning |
|---|---|---|
| `min_rsrp` | `-100.0` | Minimum acceptable RSRP (dBm). |
| `min_rsrq` | `-11.0` | Minimum acceptable RSRQ (dB). |
| `min_sinr` | `0.0` | Minimum acceptable SINR (dB). |

## `[backend]`

Shared HTTPS destination and auth for everything this device sends to the backend server
(heartbeat and measurement uploads alike). `url` must be `https` — the device initiates every
contact and the backend's response is a path for instructions to reach the device, so the
transport has to be one the device can trust. `[heartbeat]` and `[uploader]` each append their own
path onto this `url` rather than configuring a host of their own.

| Field | Default | Meaning |
|---|---|---|
| `url` | `""` | Backend base URL. Per-device identity — set in `config.local.toml`. |
| `device_key` | `""` | Per-device API key for authenticating requests. Per-device secret — set in `config.local.toml`. |

## `[heartbeat]`

Periodic report to the backend on how the current run is going. Its response can carry
HMAC-signed, allow-listed config overrides and one-shot remote commands.

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turns the heartbeat on. |
| `path` | `"/heartbeat"` | Path appended to `[backend] url`. |
| `interval_s` | `60.0` | Seconds between heartbeats. |
| `timeout_s` | `10.0` | HTTP timeout for a heartbeat POST. |
| `device_id` | `""` | Device identifier sent with each heartbeat, distinct from `[device] device_id`. Per-device identity — set in `config.local.toml`. |
| `hmac_secret` | `""` | Verifies that config overrides/commands riding on the heartbeat response really came from the backend. Leave unset to keep remote commands disabled (a response claiming to carry them is then discarded and logged). Per-device secret — set in `config.local.toml`. |

## `[selftest]`

Backend destination for `python -m measurement_software.selftest` check results. Reuses
`[backend] device_key` and `[heartbeat] device_id` for authentication — no separate credential.

| Field | Default | Meaning |
|---|---|---|
| `results_url` | `""` | Where to POST selftest results. Empty by default: an unconfigured device runs checks locally (console output) only. Per-device identity (embeds the real backend host) — set in `config.local.toml`. |

## `[gps_fix_test]`

Live GPS-fix-finding diagnostic (`run-gps-fix-test-now` / `stop-gps-fix-test-now`), for finding a
good antenna mounting position. Only dispatched while in Waiting Mode (see `[system] mode` below).

| Field | Default | Meaning |
|---|---|---|
| `status_url` | `""` | Where to POST `num_satellites`/`has_fix`/`elapsed_s` roughly every `report_interval_s`. Per-device identity — set in `config.local.toml`. |
| `report_interval_s` | `2.0` | Status POST cadence while the diagnostic is running. |
| `timeout_s` | `300.0` | Auto-stops the run if `stop-gps-fix-test-now` never arrives. |

## `[storage]`

Local upload-backlog safety net. Not a cap or eviction policy — just a loud warning if free space
runs low, in case a connectivity gap runs much longer than expected.

| Field | Default | Meaning |
|---|---|---|
| `low_free_space_warning_bytes` | `500_000_000` | Free-space threshold on `[uploader] upload_dir`'s filesystem that triggers a warning. |

## `[fan]`

Case fan on/off control via GPIO, based on CPU temperature with hysteresis. Disabled by default.

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turns fan control on. |
| `gpio_pin` | `27` | BCM pin number, same numbering as `[system] shutdown_gpio`. |
| `temp_on_celsius` | `70.0` | CPU temperature that turns the fan on. |
| `temp_off_celsius` | `60.0` | CPU temperature that turns the fan off. Kept distinct from `temp_on_celsius` to avoid rapidly toggling right at a single boundary. |
| `poll_interval_s` | `5.0` | CPU temperature poll interval. |

## `[display]`

Small SSD1306 OLED status display over I2C, for field diagnostics with no terminal attached.
Optional and disabled by default: an absent section, or `enabled = false`, uses a no-op display so
the app runs unchanged without a screen attached.

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turns the display on. |
| `type` | `"ssd1306"` | Selects the `Display` implementation via `displays/__init__.py`. Only `"ssd1306"` is registered today. |
| `i2c_port` | `1` | I2C bus number. |
| `i2c_address` | `0x3C` | I2C address of the display. |
| `refresh_interval_s` | `2.0` | Screen refresh cadence. |

## `[latency_test]`

Active latency measurements via `flent`, run against a test server while the vehicle is moving.
Disabled by default: the tests need a server-side counterpart (`netperf` for the load test, a host
that answers pings for the baseline) that is outside this repo's control, so an unconfigured
device must never start generating test traffic on its own.

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turns latency testing on. |
| `host` | `""` | Test server hostname. Per-device identity — set in `config.local.toml`. |
| `flent_binary` | `"flent"` | `flent` binary name/path. |
| `baseline_test` | `"ping"` | flent test name for the cheap, nearly-free baseline (idle-link latency). |
| `baseline_interval_s` | `60.0` | How often the baseline test runs. |
| `baseline_length_s` | `10` | Baseline test duration. |
| `load_test` | `"rrul"` | flent test name for the under-load test. Deliberately saturates the link for its whole length, hence a far sparser cadence than the baseline. |
| `load_interval_s` | `900.0` | How often the load test runs. |
| `load_length_s` | `60` | Load test duration. |
| `poll_interval_s` | `30.0` | How often the tester wakes to check whether either test has come due. |
| `movement_window_s` | `30.0` | How recently the vehicle must have moved (per `[collector] position_threshold` events) for a due test to actually run; a due test whose vehicle has been still longer than this waits for the next tick. |
| `timeout_s` | `300.0` | Timeout for a single test run. |

## `[uploader]` (required)

Local storage and HTTPS upload paths for saved measurements. Uploads go over HTTPS POST to
`[backend] url`'s scheme and host, with this section's own path swapped in — there is no separate
upload host/port to configure.

| Field | Default | Meaning |
|---|---|---|
| `upload_dir` | — | Local directory where pending measurement files are staged before upload. |
| `path` | `"/upload"` | Endpoint for measurement files — one POST per *file* (a batch array). |
| `latency_path` | `"/upload/latency"` | Endpoint for latency results — takes one POST per *record* (a bare JSON object), not a batch array; the two endpoints carry incompatible schemas. |
| `debug_upload` | `true` | Binds the upload connection to `wwan0`'s IP specifically (for testing over the cellular link itself) and adds verbose logging. |
| `timeout_s` | `10.0` | HTTP timeout per upload request. |
| `upload_user` / `upload_host` / `upload_port` / `remote_dir` | `""` / `""` / `22` / `""` | SCP settings used only by the selftest uploader check, not by the main HTTPS upload path. |

## `[logging]`

| Field | Default | Meaning |
|---|---|---|
| `level` | `"INFO"` | Log level. |
| `log_file` | placeholder path | Rotating log file path. |
| `max_bytes` | `5_000_000` | Size before the log file rotates. |
| `backup_count` | `3` | Number of rotated log files kept. |

## `[system]`

Raspberry Pi hardware settings: GPIO shutdown signalling and network interfaces to report.

| Field | Default | Meaning |
|---|---|---|
| `running_on_pi` | `true` | Gates all GPIO/shutdown behavior; a local dev checkout should set this `false`. |
| `shutdown_gpio` | `17` | BCM pin used to signal run completion. |
| `network_interfaces` | `("wlan0", "wwan0")` | Interfaces checked for an IPv4 address before an upload attempt. |
| `mode` | `"auto"` | `"auto"` (default) waits battery-efficiently for confirmed vehicle movement before opening the modem/`quectel-CM`. `"waiting"` opens the cellular link and starts heartbeating immediately at boot instead, then does nothing further until an explicit remote command (`start-measuring-now`) says what to do. Only takes effect on the next boot — switched remotely via the `mode-waiting-now`/`mode-auto-now` one-shot commands, which persist to a local `mode_override.toml` rather than mutating this field mid-run. Per-device identity — set in `config.local.toml` if you want a non-default starting mode. |

## Startup validation

`load_config()` raises `ValueError` at startup (before anything hardware-facing happens) for:

- Two GPIO consumers configured to the same pin (`[system] shutdown_gpio`, `[fan] gpio_pin`, the
  display's fixed I2C pins, and the GNSS receiver's fixed UART pins if `[gnss_receiver] port` is
  an onboard UART).
- Only one of `[movement_gate] home_latitude`/`home_longitude` set.
- `[system] mode` set to anything other than `"auto"` or `"waiting"`.

It logs (but doesn't raise on) unset per-device identity/secret fields — `[backend] url`/
`device_key`, `[heartbeat] device_id`, `[quectel_cm] binary`, and (if `[heartbeat] enabled`)
`[heartbeat] hmac_secret` — since a real deployment needs them but a local dev checkout with
`[system] running_on_pi = false` is expected to be unconfigured.
