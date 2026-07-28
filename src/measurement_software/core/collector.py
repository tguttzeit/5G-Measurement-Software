import logging
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from math import radians, sin, cos, asin, sqrt
from typing import Iterator

from measurement_software.core.config import CollectorConfig
from measurement_software.core.heartbeat_sender import HeartbeatSender
from measurement_software.core.keep_modem_alive_sender import KeepModemAliveSender
from measurement_software.core.run_status import RunStatusTracker
from measurement_software.gnss.gnss_receiver import GNSSReceiver, Position, GNSSFix
from measurement_software.modems.modem import Modem, CellSample


@dataclass
class Datapoint:
    """A single cell-info sample paired with the GNSS fix and timestamp it was captured at."""

    timestamp: str
    fix: GNSSFix
    cell_sample: CellSample


class Collector:
    """Runs a GPS-triggered measurement session, sampling the modem whenever the device moves."""

    def __init__(self, modem: Modem, gnss_receiver: GNSSReceiver, config: CollectorConfig,
                 run_status: RunStatusTracker, heartbeat: HeartbeatSender):
        self._logger = logging.getLogger(__name__)
        self._modem = modem
        self._gnss_receiver = gnss_receiver
        self._run_status = run_status
        self._heartbeat = heartbeat

        self._position_threshold = config.position_threshold
        self._max_idle_time = config.max_idle_time
        self._max_wait_for_first_fix = config.max_wait_for_first_fix
        self._wait_log_interval = config.wait_log_interval
        self._gps_enabled = config.gps_enabled
        self._gps_disabled_poll_interval_s = config.gps_disabled_poll_interval_s

        self._keep_modem_alive = KeepModemAliveSender(
            config.keep_alive_host, config.keep_alive_port, config.keep_alive_interval_s
        )

        self._last_pos: Position | None = None
        self._last_movement_time: float | None = None

    def collect(self) -> list[Datapoint]:
        """Waits for a GPS fix, then collects datapoints until movement stops for too long."""
        self._modem.open()
        self._gnss_receiver.open()
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
            if first_fix is None:
                return []

            datapoints: list[Datapoint] = []
            self._last_pos = None
            self._last_movement_time = None

            for fix in self._iter_fixes(first_fix):
                if self._should_capture(fix):
                    datapoints.extend(self._capture_datapoints(fix))

                if self._has_been_idle_too_long():
                    break

        finally:
            self._heartbeat.stop()
            self._keep_modem_alive.stop()
            self._gnss_receiver.close()
            self._modem.close()
            self._logger.info("Finished data collection")

        return datapoints

    def _wait_for_first_fix(self) -> GNSSFix | None:
        """Blocks until the receiver reports a fix, or returns None if none arrives in time."""
        start = time.time()
        last_wait_log = 0.0

        while True:
            elapsed = time.time() - start
            if elapsed > self._max_wait_for_first_fix:
                self._logger.info("No GPS fix in specified interval of %s s. - Aborting.",
                                  self._max_wait_for_first_fix)
                return None

            now = time.time()
            if (now - last_wait_log) >= self._wait_log_interval:
                self._logger.info("Waiting for first GPS fix… (for %s s by now)", int(elapsed))
                last_wait_log = now

            fix = self._gnss_receiver.read_fix()
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
            fix = self._gnss_receiver.read_fix()
            if fix is not None:
                yield fix

    def _should_capture(self, fix: GNSSFix) -> bool:
        """Decides whether to query the modem for this fix.

        With GPS disabled there's no real position to gate on — haversine distance between
        identical placeholder fixes is always 0 — so every paced fix captures unconditionally
        instead of going through movement-threshold gating. Unlike a genuine movement, this never
        resets the idle clock, so max_idle_time still bounds how long a GPS-disabled run lasts.
        """
        if not self._gps_enabled:
            return True
        return self._has_moved_enough(fix)

    def _has_moved_enough(self, fix: GNSSFix) -> bool:
        """Returns True and updates the reference position if the fix cleared the movement threshold."""
        if self._last_pos is None:
            self._last_pos = fix.position
            return True

        dist = Collector.haversine(self._last_pos, fix.position)
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

    @staticmethod
    def haversine(old_position: Position, new_position: Position) -> float:
        """Great-circle distance between two positions, in meters."""
        r = 6371000
        d_latitude = radians(new_position.latitude - old_position.latitude)
        d_longitude = radians(new_position.longitude - old_position.longitude)
        a = sin(d_latitude / 2) ** 2
        b = cos(radians(old_position.latitude)) * cos(radians(new_position.latitude)) * sin(d_longitude / 2) ** 2
        return 2 * r * asin(sqrt(a + b))

    def _capture_datapoints(self, fix: GNSSFix) -> list[Datapoint]:
        """Queries the modem and pairs each cell sample with the given fix and current timestamp."""
        timestamp = f"{datetime.now(UTC).isoformat()}Z"
        samples = self._modem.query_cell_info()
        self._run_status.record_capture(samples)

        datapoints = [Datapoint(timestamp=timestamp, fix=fix, cell_sample=sample) for sample in samples]
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
