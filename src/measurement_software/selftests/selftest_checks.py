import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from measurement_software.core import system
from measurement_software.core.config import AppConfig, UploaderConfig
from measurement_software.remote.heartbeat import HeartbeatSender
from measurement_software.core.system import KeepModemAliveSender
from measurement_software.core.uploader import Uploader
from measurement_software.core.util import utc_timestamp
from measurement_software.displays import create_display
from measurement_software.selftests.selftest_results import CheckResult, CheckStatus
from measurement_software.status.backend_status import RunStatusTracker, StorageStatusReporter
from measurement_software.gnss import create_gnss_receiver
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver
from measurement_software.gnss.null_gnss_receiver import NullGNSSReceiver
from measurement_software.modems import create_modem

# Every check submitted to the backend's `POST /api/testing/results` - the other checks are
# checked and reported to the console only.
BACKEND_TEST_TYPES = frozenset({"modem", "gps", "collector", "uploader", "heartbeat"})

# How long to keep retrying read_fix() before giving up on a fix. A GNSS receiver cycles through
# several NMEA sentence types per second (GSA/GSV/GLL/RMC/... alongside GGA) and read_fix() reads
# only whatever line comes next - a single call has a real chance of landing on a non-GGA line
# even with a genuinely live fix. Confirmed live: a bare single-call check flip-flopped
# PASS/FAIL against a real, continuously-available fix. Collector._wait_for_first_fix() already
# loops for exactly this reason; these checks need the same pattern.
_GPS_FIX_WAIT_TIMEOUT_S = 5.0


def _wait_for_fix(gnss: GNSSReceiver, timeout_s: float = _GPS_FIX_WAIT_TIMEOUT_S) -> GNSSFix | None:
    """Retries read_fix() until it returns a fix or timeout_s elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        fix = gnss.read_fix()
        if fix is not None:
            return fix
    return None


def check_modem(config: AppConfig) -> CheckResult:
    """Opens the real modem and issues one cell-info query, closing it either way."""
    start = time.monotonic()
    modem = create_modem(config.modem)
    try:
        modem.open()
        samples = modem.query_cell_info()
    except Exception as e:
        return _failed("modem", start, str(e))
    finally:
        modem.close()
    return _passed("modem", start, details={"cell_samples": len(samples)})


def check_gps(config: AppConfig) -> CheckResult:
    """Opens the real GNSS receiver and waits for a GGA fix, closing it either way."""
    start = time.monotonic()
    gnss = create_gnss_receiver(config.gnss_receiver)
    try:
        gnss.open()
        fix = _wait_for_fix(gnss)
    except Exception as e:
        return _failed("gps", start, str(e))
    finally:
        gnss.close()
    if fix is None:
        return _failed("gps", start, "No GPS fix available (no valid GGA sentence received in time).")
    return _passed("gps", start, details={"num_satellites": fix.num_satellites})


def check_collector(config: AppConfig) -> CheckResult:
    """Verifies the modem and GNSS receiver can be paired into one datapoint, without recording it.

    Deliberately does not run Collector.collect() or write to the run log: doing so would leave
    selftest-generated data in the real upload backlog, indistinguishable from a genuine
    measurement once uploaded.
    """
    start = time.monotonic()
    modem = create_modem(config.modem)
    gnss = (
        create_gnss_receiver(config.gnss_receiver) if config.collector.gps_enabled else NullGNSSReceiver()
    )
    try:
        modem.open()
        gnss.open()
        fix = _wait_for_fix(gnss)
        samples = modem.query_cell_info()
    except Exception as e:
        return _failed("collector", start, str(e))
    finally:
        gnss.close()
        modem.close()
    if fix is None:
        return _failed("collector", start, "No GPS fix available to pair with a cell measurement.")
    return _passed("collector", start, details={"num_satellites": fix.num_satellites, "cell_samples": len(samples)})


def check_uploader(config: AppConfig, *, confirm_side_effects: bool) -> CheckResult:
    """Checks network reachability for uploads; with confirmation, also uploads one throwaway file."""
    start = time.monotonic()
    if not (Uploader._is_interface_up("wlan0") or Uploader._is_interface_up("wwan0")):
        return _failed("uploader", start, "No network interface (wlan0/wwan0) has an IPv4 address.")
    if not confirm_side_effects:
        return _passed("uploader", start, details={"network_reachable": True, "real_upload_attempted": False})
    return _upload_throwaway_probe(config.uploader, start)


def _upload_throwaway_probe(config: UploaderConfig, start: float) -> CheckResult:
    """Uploads one small, clearly-marked throwaway file via scp, never touching upload_dir.

    Kept entirely out of upload_dir's own *.json glob so a failed probe can never be picked up
    and retried by the normal upload_pending_files() sweep.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        probe = Path(tmp_dir) / f"selftest_probe_{utc_timestamp()}.json"
        probe.write_text('{"selftest": true}')
        remote = f"{config.upload_user}@{config.upload_host}:{config.remote_dir}{probe.name}"
        proc = subprocess.run(
            ["scp", "-P", str(config.upload_port), str(probe), remote], capture_output=True, text=True,
        )
    if proc.returncode == 0:
        return _passed("uploader", start, details={"network_reachable": True, "real_upload_attempted": True})
    return _failed("uploader", start, f"scp exited with code {proc.returncode}")


def check_heartbeat(config: AppConfig) -> CheckResult:
    """Sends one real heartbeat POST using the configured heartbeat destination."""
    start = time.monotonic()
    if not config.heartbeat.enabled:
        return _skipped("heartbeat", start, "heartbeat.enabled is false in config.toml")
    tracker = RunStatusTracker(config.run_status)
    storage = StorageStatusReporter(Path(config.uploader.upload_dir), config.storage)
    sender = HeartbeatSender(config.heartbeat, config.backend, tracker, storage)
    if not sender.is_usable():
        return _failed("heartbeat", start, "heartbeat backend URL must be https:// (see logs for the configured value).")
    if sender.send_once(tracker.status(), storage.status()):
        return _passed("heartbeat", start, details={"url": sender._url})
    return _failed("heartbeat", start, "Heartbeat POST failed or the backend is unreachable (see logs).")


def check_keep_modem_alive(config: AppConfig) -> CheckResult:
    """Starts the UDP keep-alive sender briefly, confirming its background thread runs cleanly."""
    start = time.monotonic()
    sender = KeepModemAliveSender(
        config.collector.keep_alive_host, config.collector.keep_alive_port, config.collector.keep_alive_interval_s,
    )
    try:
        sender.start()
        time.sleep(min(config.collector.keep_alive_interval_s, 1.0) + 0.2)
    finally:
        sender.stop()
    return _passed(
        "keep_modem_alive", start,
        details={"host": config.collector.keep_alive_host, "port": config.collector.keep_alive_port},
    )


def check_fan(config: AppConfig) -> CheckResult:
    """Reads the CPU temperature and, if fan control is enabled, briefly toggles the fan GPIO."""
    start = time.monotonic()
    try:
        temperature = system.read_cpu_temperature_celsius()
    except OSError as e:
        return _failed("fan", start, str(e))

    details: dict = {"cpu_temperature_celsius": temperature}
    if config.fan.enabled:
        try:
            system.setup_fan_gpio(config.fan.gpio_pin)
            system.set_fan_state(config.fan.gpio_pin, True)
            time.sleep(0.3)
            system.set_fan_state(config.fan.gpio_pin, False)
        except Exception as e:
            return _failed("fan", start, str(e), details=details)
        details["gpio_toggled"] = True
    return _passed("fan", start, details=details)


def check_display(config: AppConfig) -> CheckResult:
    """Opens the configured display and shows a test message, closing it either way."""
    start = time.monotonic()
    if not config.display.enabled:
        return _skipped("display", start, "display.enabled is false in config.toml")
    display = create_display(config.display)
    try:
        display.open()
        display.show("5GM selftest OK")
    except Exception as e:
        return _failed("display", start, str(e))
    finally:
        display.close()
    return _passed("display", start)


def check_latency_tester(config: AppConfig) -> CheckResult:
    """Verifies the flent binary runs and a test host/server is configured, without running a test.

    Deliberately does not run a real flent test: the load test saturates the link for its whole
    length and spends real data, which is not something a wiring check should trigger by itself.
    """
    start = time.monotonic()
    if not config.latency_test.enabled:
        return _skipped("latency_tester", start, "latency_test.enabled is false in config.toml")
    if not config.latency_test.host:
        return _failed("latency_tester", start, "latency_test.host is not configured.")
    try:
        proc = subprocess.run(
            [config.latency_test.flent_binary, "--version"], capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return _failed("latency_tester", start, f"flent binary {config.latency_test.flent_binary!r} not found.")
    except subprocess.TimeoutExpired:
        return _failed("latency_tester", start, "flent --version timed out.")
    if proc.returncode != 0:
        return _failed("latency_tester", start, f"flent --version exited with code {proc.returncode}.")
    return _passed(
        "latency_tester", start, details={"flent_version": proc.stdout.strip(), "host": config.latency_test.host},
    )


def _passed(name: str, start: float, *, details: dict | None = None) -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.PASSED, duration_ms=_elapsed_ms(start), details=details)


def _failed(name: str, start: float, error_message: str, *, details: dict | None = None) -> CheckResult:
    return CheckResult(
        name=name, status=CheckStatus.FAILED, duration_ms=_elapsed_ms(start),
        error_message=error_message, details=details,
    )


def _skipped(name: str, start: float, reason: str) -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.SKIPPED, duration_ms=_elapsed_ms(start), error_message=reason)


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


_CHECKS: dict[str, Callable[[AppConfig, bool], CheckResult]] = {
    "modem": lambda config, confirm_side_effects: check_modem(config),
    "gps": lambda config, confirm_side_effects: check_gps(config),
    "collector": lambda config, confirm_side_effects: check_collector(config),
    "uploader": lambda config, confirm_side_effects: check_uploader(config, confirm_side_effects=confirm_side_effects),
    "heartbeat": lambda config, confirm_side_effects: check_heartbeat(config),
    "keep_modem_alive": lambda config, confirm_side_effects: check_keep_modem_alive(config),
    "fan": lambda config, confirm_side_effects: check_fan(config),
    "display": lambda config, confirm_side_effects: check_display(config),
    "latency_tester": lambda config, confirm_side_effects: check_latency_tester(config),
}


def available_checks() -> list[str]:
    """Names of every registered selftest check, in dispatch order."""
    return list(_CHECKS)


def run_check(name: str, config: AppConfig, *, confirm_side_effects: bool) -> CheckResult:
    """Runs one named check by dispatching through the check factory-dict."""
    try:
        factory = _CHECKS[name]
    except KeyError:
        raise ValueError(f"Unknown selftest check: {name!r}")
    return factory(config, confirm_side_effects)
