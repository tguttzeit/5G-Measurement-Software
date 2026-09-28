import logging
import threading
import time
from pathlib import Path
from typing import Iterator

from measurement_software.core.config import CollectorConfig, DeviceConfig
from measurement_software.core.datapoint import Datapoint
from measurement_software.remote.heartbeat import HeartbeatSender
from measurement_software.core.movement_tracker import MovementTracker
from measurement_software.core.run_log import RunLog
from measurement_software.core.system import KeepModemAliveSender
from measurement_software.core.util import haversine_distance_m, utc_timestamp
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position
from measurement_software.modems.modem import Modem
from measurement_software.status.backend_status import GpsFixStatusTracker, RunStatusTracker


class RemoteShutdownRequested(Exception):
    """Raised to unwind an active collection run after a remote shutdown-now command."""


class Collector:
    """Runs a GPS-triggered measurement session, sampling the modem whenever the device moves."""

    def __init__(self, modem: Modem, gnss_receiver: GNSSReceiver, config: CollectorConfig,
                 run_status: RunStatusTracker, heartbeat: HeartbeatSender, run_log: RunLog[Datapoint],
                 device: DeviceConfig, gps_fix_status: GpsFixStatusTracker | None = None):
        self._logger = logging.getLogger(__name__)
        self._modem = modem
        self._gnss_receiver = gnss_receiver
        self._run_status = run_status
        self._heartbeat = heartbeat
        self._run_log = run_log
        self._device_id = device.device_id
        self._mission_type = device.mission_type
        self._gps_fix_status = gps_fix_status

        self._position_threshold = config.position_threshold
        self._max_idle_time = config.max_idle_time
        self._max_wait_for_first_fix = config.max_wait_for_first_fix
        self._wait_log_interval = config.wait_log_interval
        self._gps_enabled = config.gps_enabled
        self._gps_disabled_poll_interval_s = config.gps_disabled_poll_interval_s

        self._keep_modem_alive = KeepModemAliveSender(
            config.keep_alive_host, config.keep_alive_port, config.keep_alive_interval_s
        )

        self._movement = MovementTracker()
        self._last_pos: Position | None = None
        self._last_movement_time: float | None = None

        self._remote_lock = threading.Lock()
        self._shutdown_requested = False
        self._sim_reset_requested = False

    @property
    def movement(self) -> MovementTracker:
        """Where the vehicle last moved, for anything running alongside the collection loop."""
        return self._movement

    def request_shutdown(self) -> None:
        """Asks the running collection loop to stop as soon as it next checks in.

        Thread-safe: called from the heartbeat thread when a remote shutdown-now command
        arrives, while the collection loop itself runs on a different thread.
        """
        with self._remote_lock:
            self._shutdown_requested = True

    def request_sim_reset(self) -> None:
        """Asks the running collection loop to reconnect the modem as soon as it next checks in.

        Thread-safe, same reasoning as request_shutdown(). The reconnect itself always happens
        on the collection thread, never here, since the modem's serial connection isn't
        thread-safe.
        """
        with self._remote_lock:
            self._sim_reset_requested = True

    def _check_remote_commands(self) -> None:
        """Applies a pending remote sim-reset in place; raises if a shutdown was requested.

        Called once per loop iteration on the collection thread only, so a modem reconnect
        never races the loop's own use of the modem.
        """
        with self._remote_lock:
            shutdown_requested = self._shutdown_requested
            sim_reset_requested = self._sim_reset_requested
            self._sim_reset_requested = False

        if sim_reset_requested:
            self._logger.warning("Remote sim-reset-now command received - reconnecting modem.")
            self._modem.close()
            self._modem.open()

        if shutdown_requested:
            self._logger.critical("Remote shutdown-now command received - ending collection.")
            raise RemoteShutdownRequested()

    def collect(self) -> Path:
        """Waits for a GPS fix, then records datapoints until movement stops for too long.

        Returns the run log written, which holds everything captured up to the moment the
        session ended - including a session ended by the power being cut.
        """
        self._modem.open()
        self._gnss_receiver.open()
        self._run_log.open()
        time.sleep(1)

        self._keep_modem_alive.start()
        self._heartbeat.start()
        self._logger.info("Starting data collection...")
        if not self._gps_enabled:
            self._logger.warning(
                "GPS disabled (config.collector.gps_enabled=false) — using placeholder fixes, "
                "not real GNSS hardware. Testing mode only."
            )

        try:
            first_fix = self._wait_for_first_fix()
            if first_fix is not None:
                self._record_until_idle(first_fix)
        finally:
            self._heartbeat.stop()
            self._keep_modem_alive.stop()
            self._run_log.close()
            self._gnss_receiver.close()
            self._modem.close()
            self._logger.info("Finished data collection")

        return self._run_log.path

    def _record_until_idle(self, first_fix: GNSSFix) -> None:
        """Records a datapoint for every qualifying movement until the device sits still for too long."""
        self._last_pos = None
        self._last_movement_time = None

        for fix in self._iter_fixes(first_fix):
            self._check_remote_commands()

            if self._should_capture(fix):
                self._movement.record_movement(fix)
                self._run_log.append(self._capture_datapoints(fix))

            if self._has_been_idle_too_long():
                break

    def _wait_for_first_fix(self) -> GNSSFix | None:
        """Blocks until the receiver reports a fix, or returns None if none arrives in time."""
        start = time.time()
        last_wait_log = 0.0

        while True:
            self._check_remote_commands()

            elapsed = time.time() - start
            if elapsed > self._max_wait_for_first_fix:
                self._logger.info("No GPS fix in specified interval of %s s. - Aborting.",
                                  self._max_wait_for_first_fix)
                return None

            now = time.time()
            if (now - last_wait_log) >= self._wait_log_interval:
                self._logger.info("Waiting for first GPS fix… (for %s s by now)", int(elapsed))
                last_wait_log = now

            fix = self._read_fix()
            if fix is not None:
                return fix

    def _iter_fixes(self, first_fix: GNSSFix) -> Iterator[GNSSFix]:
        """Yields the first fix, then every subsequent non-None fix from the receiver, forever.

        With GPS disabled, the receiver has no natural read pace of its own (unlike blocking on
        real serial I/O), so this paces reads itself at gps_disabled_poll_interval_s.
        """
        yield first_fix
        while True:
            if not self._gps_enabled:
                time.sleep(self._gps_disabled_poll_interval_s)
            fix = self._read_fix()
            if fix is not None:
                yield fix

    def _read_fix(self) -> GNSSFix | None:
        """Reads one fix, recording it into the shared gps_fix_status tracker if one is wired."""
        fix = self._gnss_receiver.read_fix()
        if fix is not None and self._gps_fix_status is not None:
            self._gps_fix_status.record_fix(fix)
        return fix

    def _should_capture(self, fix: GNSSFix) -> bool:
        """Decides whether to query the modem for this fix.

        With GPS disabled there's no real position to gate on — haversine distance between
        identical placeholder fixes is always 0 — so every paced fix captures unconditionally
        instead of going through movement-threshold gating. Unlike a genuine movement, this never
        resets the idle clock, so max_idle_time still bounds how long a GPS-disabled run lasts.

        A GPS-disabled run therefore reads as continuously moving to anything watching the
        movement record, which is what that testing mode wants: the rest of the pipeline should
        run as it would on the road, without real GNSS hardware attached.
        """
        if not self._gps_enabled:
            return True
        return self._has_moved_enough(fix)

    def _has_moved_enough(self, fix: GNSSFix) -> bool:
        """Returns True and updates the reference position if the fix cleared the movement threshold."""
        if self._last_pos is None:
            self._last_pos = fix.position
            return True

        dist = haversine_distance_m(self._last_pos, fix.position)
        if dist < self._position_threshold:
            self._logger.debug(
                "No relevant movement detected: %.1f m (< %s m (specified threshold))",
                dist, self._position_threshold,
            )
            return False

        self._logger.info("Enough movement detected: %.1f m", dist)
        self._last_pos = fix.position
        self._last_movement_time = None
        return True

    def _capture_datapoints(self, fix: GNSSFix) -> list[Datapoint]:
        """Queries the modem and pairs each cell sample with the given fix and current timestamp."""
        timestamp = utc_timestamp()
        samples = self._modem.query_cell_info()
        self._run_status.record_capture(samples)

        datapoints = [
            Datapoint(
                timestamp=timestamp,
                device_id=self._device_id,
                mission_type=self._mission_type,
                fix=fix,
                cell_sample=sample,
            )
            for sample in samples
        ]
        for datapoint in datapoints:
            self._logger.debug("Datapoint captured: %s", datapoint)
        return datapoints

    def _has_been_idle_too_long(self) -> bool:
        """Returns True once the device has gone without movement for longer than max_idle_time."""
        if self._last_movement_time is None:
            self._last_movement_time = time.time()
            return False

        idle_duration = time.time() - self._last_movement_time
        if idle_duration >= self._max_idle_time:
            self._logger.info("No movement for %s s. Ending data collection.", self._max_idle_time)
            return True
        return False
