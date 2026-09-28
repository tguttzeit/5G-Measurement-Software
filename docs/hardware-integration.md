# Hardware integration guide

This app talks to three swappable hardware roles — modem, GNSS receiver, and status display —
each behind an abstract interface with a small factory that picks a concrete implementation by a
`config.type` string. Adding support for a different modem, GNSS receiver, or display means
writing one new implementation class and registering it; nothing else in the app needs to change.

## The pattern

For each role there are three pieces:

1. **An ABC** defining the contract (e.g. `modems/modem.py::Modem`).
2. **One or more concrete implementations** (e.g. `modems/quectel.py::Quectel`).
3. **A factory dict** keyed by `config.type` (e.g. `modems/__init__.py::_MODEM_FACTORIES`), used
   by `create_modem(config)` to build the configured implementation. An unrecognized `type` raises
   `ValueError`.

`main.py` only ever calls `create_modem(config.modem)` / `create_gnss_receiver(config.gnss_receiver)`
/ `create_display(config.display)` — it never imports a concrete implementation directly. Adding a
new one is purely additive: a new file plus one new factory-dict entry.

## Adding a new modem

1. Add a dataclass for anything the new modem needs beyond `ModemConfig`'s existing fields
   (`port`, `baud_rate`, `timeout`, `mode`, retry settings), if any — most serial modems need
   nothing extra.
2. Implement `Modem` (`modems/modem.py`) in a new `modems/<name>.py`:
   - `open()` / `close()` — manage the serial connection.
   - `unlock_sim()` — enter the SIM PIN if required. Raise the matching `SimUnlockError` subclass
     on failure (`SimPinNotConfiguredError`, `SimPinRejectedError`, `SimPukRequiredError`,
     `SimStatusUnknownError`) rather than a bare exception, so callers can react appropriately.
     **Never retry a rejected PIN automatically, and never attempt PUK entry** — see the
     docstrings on those exceptions; both risk permanently locking the SIM.
   - `query_cell_info() -> list[CellSample]` — return one `CellSample` per active RAT. Populate
     whatever fields the modem actually reports; leave the rest `None`. `vendor_specific_extras`
     is an open `dict` for anything that doesn't map onto the common fields.
   - `power_down()` — send the modem's power-down command.
3. Register it in `modems/__init__.py`'s `_MODEM_FACTORIES` under a new `type` string.
4. Set `[modem] type = "<name>"` in `config.toml`.
5. Write tests under `tests/modems/test_<name>.py`, following `tests/modems/test_quectel.py`: patch
   `serial.Serial` at the point of use (`measurement_software.modems.<name>.serial.Serial`) with a
   fake that records what was written and returns scripted response bytes — never touch a real
   serial port in a test.

## Adding a new GNSS receiver

1. Implement `GNSSReceiver` (`gnss/gnss_receiver.py`) in a new `gnss/<name>.py`:
   - `open()` / `close()` — manage the connection.
   - `read_fix() -> GNSSFix | None` — the latest fix (`Position` + satellite count used), or
     `None` if none is currently available. Set `GNSSFix.placeholder = True` only for a
     deliberately fake fix (e.g. a GPS-disabled testing run) — never for real data.
   - `read_datetime() -> datetime | None` — the latest GNSS-derived UTC time, used to opportunistically
     sync the system clock.
   - `read_satellites_in_view()` is optional (default returns `None`): override it only if the
     receiver exposes a tracked-satellite count independent of having a fix yet — that's what the
     GPS-fix-finding field diagnostic uses to show progress before a fix exists.
2. Register it in `gnss/__init__.py`'s `_GNSS_FACTORIES` under a new `type` string.
3. Set `[gnss_receiver] type = "<name>"` in `config.toml`. If the new receiver uses one of the
   Pi's onboard UART ports (`/dev/serial0`, `/dev/ttyAMA0`, `/dev/ttyS0`), `core/config.py`'s
   startup GPIO-conflict validation already claims GPIO14/15 for it automatically — no extra
   config needed for that check to work.
4. Write tests under `tests/gnss/test_<name>.py`, following `tests/gnss/test_nmea_serial.py`'s
   `serial.Serial`-patching approach.

## Adding a new display

1. Implement `Display` (`displays/display.py`) in a new `displays/<name>.py`. Follow
   `displays/ssd1306_display.py` for the shape of the interface; `displays/null_display.py` shows
   the no-op baseline every disabled/unconfigured device falls back to.
2. Register it in `displays/__init__.py`'s `_DISPLAY_FACTORIES` under a new `type` string.
   `create_display()` returns `NullDisplay()` whenever `[display] enabled = false`, regardless of
   `type` — a new implementation only has to handle the enabled case.
3. Set `[display] type = "<name>"` and any new fields your implementation needs on `DisplayConfig`
   (`core/config.py`) — e.g. a different bus/address scheme than `i2c_port`/`i2c_address`.
4. If the display claims fixed GPIO pins (as the SSD1306's I2C pins do), add the same kind of
   claim to `core/config.py::_validate_gpio_pins()` so a real pin conflict fails fast at startup
   instead of at runtime.
5. Write tests under `tests/displays/test_<name>.py`, following `tests/displays/test_ssd1306_display.py`.

## General notes

- Config fields specific to one hardware implementation belong on that role's config dataclass
  (`ModemConfig`, `GnssConfig`, `DisplayConfig`) in `core/config.py`, documented in
  [`configuration.md`](configuration.md).
- Anything per-device (a real hostname, credential, or hardware address that shouldn't live in the
  checked-in `config.toml`) belongs in `config.local.toml` instead — see the allow-list in
  `core/config_sources.py::ALLOWED_LOCAL_FIELDS` and [`device-bringup-faq.md`](device-bringup-faq.md).
- See [`architecture.md`](architecture.md) for how these pieces fit into the rest of the run
  lifecycle, and the top-level [`testing patterns`](architecture.md#testing-patterns) for the
  project's general approach to faking hardware boundaries in tests.
