# Configuration reference

`config.toml` at the repo root is read by path relative to the installed package, not the current
working directory, and is loaded by `core/config.py::load_config()`. Sections marked **required**
raise `KeyError` if missing; everything else falls back to the dataclass defaults below if the
section (or field) is absent from `config.toml`.

Layering, later wins: dataclass defaults, `config.toml`, the `[profiles.*]` table for the device's
`mission_type` (see [`[profiles]`](#profiles)), `config.local.toml` (per-device identity/secrets set by hand once at setup — see
[`device-bringup-faq.md`](device-bringup-faq.md)), then `config_overrides.toml` (remote runtime
tuning pushed by the backend). A field can only ever appear in one of the last two: identity/secret
fields are excluded from remote override entirely, enforced at import time in `config_local.py`.

A value proven out in `config_overrides.toml` can be made the new permanent baseline with
`python -m measurement_software.promote_overrides` (or the remote `promote-overrides-now`
command, dispatched the same signed way as every other one-shot command) — it writes each
currently-set override field into its matching `config.toml` location (via the
`OVERRIDE_FIELD_TARGETS` mapping in `core/config_sources.py`, comment-preserving since it edits
`config.toml` with `tomlkit`) and clears it from `config_overrides.toml`, so the device reverts to
behaving as if the override had never been applied. Like every other `config_overrides.toml`
change, this only takes effect from the *next* `load_config()` call (the next boot).

## Remote overrides (`config_overrides.toml`)

`FIELD_SPECS` in `core/config_sources.py` is the allow-list: a field is remotely settable if and
only if it is listed there, with a type and range. Anything else, and any value of the wrong type
or outside its range, is dropped with a log line while the rest of the batch is still applied.
Types are strict: `bool` fields take exactly `true`/`false`, `int` fields reject fractional
values, float fields reject `NaN`/`inf`, enums match by exact string. Overridable fields:

| Group | Fields |
|---|---|
| Timing and gates | `idle_threshold_s` (`0` = never end on idle), `day_end_threshold_s` (accepted, no device behavior yet), `heartbeat_interval_s`, `collector_*` (`capture_gate` `distance`/`time`, `position_threshold`, `capture_interval_s`, `max_wait_for_first_fix`, `wait_log_interval`, `gps_enabled`, `gps_disabled_poll_interval_s`), `movement_gate_*` (`enabled`, `poll_interval_s`, `movement_threshold`, `confirmations_required`, `home_latitude`/`home_longitude`, `home_departure_threshold`), `latency_test_*` (`enabled`, three intervals), `run_status_empty_captures_until_pipeline_broken` |
| Device | `system_mode` (`auto`/`waiting`), `device_mission_type` (`ground`/`drone`), `display_enabled`, `display_refresh_interval_s` |
| Modem, GNSS, GPIO | `modem_mode` (`serving_cell`/`sa_scan`), `modem_sim_unlock_poll_attempts`, `modem_sim_unlock_poll_interval`, `gnss_receiver_retries`, `gnss_receiver_retry_delay_s`, `system_running_on_pi`, `system_shutdown_gpio` |

The home position is nullable: a JSON `null` overrides it back to unset, stored in the file as a
`cleared = ["movement_gate_home_latitude", ...]` list (only nullable fields are accepted there).

`system_mode` is the device mode. The `mode-waiting-now`/`mode-auto-now`/`stop-measuring-now`
commands write it here; the backend does not queue it as an override, because every queued
override is re-sent on every heartbeat and would revert those commands one heartbeat later. A
`mode_override.toml` left over from an older version is migrated into `system_mode` once at startup
(an invalid value is dropped; an existing `system_mode` wins) and then deleted.

A bad override never stops boot. The GPIO-pin, home-position and mode checks of
[Startup validation](#startup-validation) run again after overrides are applied; if an override
breaks one, the override fields involved are dropped and logged and the `config.toml` value is kept.
A corrupt `config_overrides.toml`, or a field in it that is no longer valid, is likewise ignored.

A heartbeat response carrying overrides or commands is accepted only if its signed body echoes the
random `nonce` sent in the request it answers. A captured response therefore can't be replayed
later, and the device needs no clock or persisted counter for this.

## `[modem]` (required)

Serial connection and query-mode settings for the cellular modem.

| Field | Default | Meaning |
|---|---|---|
| `type` | — | Selects the `Modem` implementation via the factory in `modems/__init__.py`. Only `"quectel"` is registered today. |
| `port` | unset | Serial device path of the modem's AT interface, e.g. `/dev/ttyUSB2`. Unset: looked up on every open via `/dev/serial/by-id/usb-Quectel_*-if02-port0`, which stays valid when the modem re-enumerates on USB and its `/dev/ttyUSBn` number shifts. Opening fails if no or more than one such port exists. |
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
| `mission_type` | `"ground"` | `"ground"` or `"drone"`. Carried on every datapoint and selects the settings profile from [`[profiles]`](#profiles). |

## `[profiles]`

Per-mission-type settings, keyed by `[device] mission_type`: `[profiles.ground]` and
`[profiles.drone]`. A profile maps [remote override](#remote-overrides-config_overridestoml) field
names to values and is validated through the same `FIELD_SPECS`; an unknown field, a field outside
that allow-list (e.g. `config.local.toml` identity/secret fields, or `device_mission_type`), an
invalid value, or an unknown profile name raises `ValueError` at startup. Every profile is
validated, not only the active one. The mission type is resolved first, so a `device_mission_type`
remote override also switches the profile. Only the profile name is logged.

Precedence, later wins: dataclass defaults, `config.toml`, active profile, `config.local.toml`,
`config_overrides.toml`. A remote override can therefore still tweak a single device.

| Profile | Content |
|---|---|
| `ground` | Empty: ground behaviour is the base `config.toml` values. |
| `drone` | `movement_gate_enabled = false`, `idle_threshold_s = 0` (measure right away, never end on idle). |

Runtime components never branch on `mission_type`; all differences are ordinary config values.

## `[collector]`

Thresholds controlling when a measurement is captured and when a session ends.

| Field | Default | Meaning |
|---|---|---|
| `position_threshold` | `15` | Minimum haversine distance (meters) from the last captured position before the next measurement is taken. |
| `max_idle_time` | `200` | Seconds without qualifying movement before the collection session ends. `0` disables the idle end: the run then ends only via `stop-measuring-now`, `shutdown-now`/`reboot-now` or power loss, and the run log grows until then (only the `storage.low_free_space_warning_bytes` warning guards against a full disk). |
| `max_wait_for_first_fix` | `300` | Seconds to wait for an initial GPS fix before giving up on the run entirely. |
| `wait_log_interval` | `5` | How often (seconds) to log a "still waiting for fix" message. |
| `keep_alive_host` | `"8.8.8.8"` | Host the background UDP keep-alive sender targets, to keep the modem's link alive during collection. |
| `keep_alive_port` | `53` | Port for the same keep-alive traffic. |
| `keep_alive_interval_s` | `4.0` | Interval between keep-alive packets. |
| `gps_enabled` | `true` | When `false`, the collector runs without a real GPS fix (placeholder fixes), for testing without GNSS hardware attached. |
| `gps_disabled_poll_interval_s` | `5.0` | Poll interval used while `gps_enabled = false`. |
| `capture_gate` | `"distance"` | Which gate decides when to capture a measurement: `"distance"` (`position_threshold`-based, the above default) or `"time"` (fixed cadence via `capture_interval_s`). Idle-timeout detection always stays distance-based regardless of this setting - see `max_idle_time` above. |
| `capture_interval_s` | `15.0` | Seconds between captures when `capture_gate = "time"`. Unused otherwise. |
| `modem_reconnect_attempts` | `10` | If the modem connection is lost mid-run (port gone, I/O error), the capture is skipped and the modem reopened up to this many times before the error ends the run. At least `1`. |
| `modem_reconnect_delay_s` | `3.0` | Seconds to wait before each reopen attempt, giving a modem that dropped off USB time to re-enumerate (about 10 s in the field). |

## `[movement_gate]`

Pre-flight wait for confirmed vehicle movement, before `Collector.collect()` starts. Runs
GNSS-only, at a low duty cycle — it never touches the modem or `quectel-CM` itself.

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | When `false`, the movement wait is skipped and measuring starts right away (e.g. for drones, which may sit on the ground or hover). `quectel-CM` and the heartbeat still start at boot. |
| `poll_interval_s` | `45.0` | How often to poll GNSS while waiting for movement. |
| `movement_threshold` | `15.0` | Minimum distance (meters) between polls to count as movement. |
| `confirmations_required` | `2` | Consecutive movement-clearing polls required before a run is considered started. |
| `home_latitude` / `home_longitude` | unset | Optional known garage/home position. Both must be set together (validated at startup) or both left unset. When set, departure from this position is additionally required before a run starts, on top of the distance-based movement detection. |
| `home_departure_threshold` | `100.0` | Distance (meters) from the home position that counts as "departed". |

## `[quectel_cm]`

The `quectel-CM` connection-manager daemon, started by this app itself (via `sudo`) right at boot
— not auto-started by systemd. Restarted by the link watchdog when the link stays down (see
`[link_watchdog]`).

| Field | Default | Meaning |
|---|---|---|
| `binary` | placeholder path | Path to the built `quectel-CM` binary. Per-device — set the real path in `config.local.toml`, never here; the shipped placeholder is intentionally never a real path. |
| `args` | `()` | Extra arguments passed to `quectel-CM`, e.g. `["-s", "web.vodafone.de"]` for the APN. |

## `[run_status]`

What the heartbeat reports about run quality. A measurement counts as "valid" if its RSRP, RSRQ and
SINR are all within the range the radio standard allows for its RAT, and "invalid" otherwise
(missing, sentinel or garbage values). How good a valid signal is gets judged when analysing the
recorded data, not on the device.

| Field | Default | Meaning |
|---|---|---|
| `empty_captures_until_pipeline_broken` | `3` | Consecutive empty captures while moving before the heartbeat reports the measurement pipeline as broken. |

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
| `success_log_interval_s` | `600.0` | Delivered heartbeats are logged at INFO: the first one, the first one after failures, and otherwise one summary line per this many seconds. |

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
| `upload_dir` | `"upload_queue"` (relative to the working directory) | Local directory where pending measurement files are staged before upload. Per-device — set the real absolute path in `config.local.toml`. |
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
| `mode` | `"auto"` | `"auto"` (default) waits battery-efficiently for confirmed vehicle movement before opening the modem/`quectel-CM`. `"waiting"` opens the cellular link and starts heartbeating immediately at boot instead, then does nothing further until an explicit remote command (`start-measuring-now`) says what to do. Only takes effect on the next boot — switched remotely via the `mode-waiting-now`/`mode-auto-now` one-shot commands, which persist `system_mode` to `config_overrides.toml` rather than mutating this field mid-run. |
| `crash_same_error_limit` | `3` | An unhandled error reboots the device. Once the same error (exception type + raising function) has occurred this many times in a row, the device switches to Waiting Mode (via `config_overrides.toml`) and reboots; an error even after that reboots it into maintenance mode (only the cellular link stays up; see `docs/architecture.md`). At least `1`. |
| `crash_reboot_limit` | `5` | The same escalation also happens once this many reboots for unhandled errors of any kind have happened in a row. At least `1`. |

## `[link_watchdog]`

Recovers a cellular link that stays down. Only active with `[system] running_on_pi`, since
`quectel-CM` is only started then.

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Turns the watchdog on. |
| `interface` | `"wwan0"` | Interface whose IPv4 address means "link up". |
| `check_interval_s` | `30.0` | Seconds between checks. |
| `restart_after_s` | `180.0` | Once the link has been down this long, `quectel-CM` is restarted - and again every this many seconds while it stays down. |
| `radio_reset_after_restarts` | `2` | After this many restarts that didn't help, the next step resets the modem's radio (`AT+CFUN=0`/`1`) instead - only possible while measuring; otherwise `quectel-CM` is restarted again. |

## Startup validation

`load_config()` raises `ValueError` at startup (before anything hardware-facing happens) for:

- Two GPIO consumers configured to the same pin (`[system] shutdown_gpio`, the display's fixed
  I2C pins, and the GNSS receiver's fixed UART pins if `[gnss_receiver] port` is an onboard UART).
- Only one of `[movement_gate] home_latitude`/`home_longitude` set.
- `[system] mode` set to anything other than `"auto"` or `"waiting"`.
- `[collector] modem_reconnect_attempts` below 1 or `modem_reconnect_delay_s` negative.
- `[system] crash_same_error_limit`/`crash_reboot_limit` below 1.
- A non-positive `[link_watchdog] check_interval_s`/`restart_after_s`, or a negative
  `radio_reset_after_restarts`.
- An invalid `[profiles.*]` table (see [`[profiles]`](#profiles)). The checks above also run on
  the config with the active profile applied.

These checks also run again after `config_overrides.toml` is applied, but there a violation drops
the offending override instead of raising (see [Remote overrides](#remote-overrides-config_overridestoml)).

It logs (but doesn't raise on) unset per-device identity/secret fields — `[backend] url`/
`device_key`, `[heartbeat] device_id`, `[quectel_cm] binary`, and (if `[heartbeat] enabled`)
`[heartbeat] hmac_secret` — since a real deployment needs them but a local dev checkout with
`[system] running_on_pi = false` is expected to be unconfigured.
