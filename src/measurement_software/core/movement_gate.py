import logging
import time

from measurement_software.core.system import ClockSync
from measurement_software.core.config import MovementGateConfig
from measurement_software.core.util import haversine_distance_m
from measurement_software.status.backend_status import GpsFixStatusTracker
from measurement_software.core.run_phase import RunPhase, RunPhaseState
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position

# A full multi-constellation NMEA report cycle (GGA, GLL, several GSA/GSV bursts per active
# constellation, RMC, VTG, ...) can run past 30 sentences before GGA comes back around - well
# past what a single-constellation module would emit. This just bounds the search per poll so a
# receiver that has genuinely gone silent doesn't spin forever; it plays no role in freshness -
# that's already guaranteed by _poll_fix() reopening the port (and thus resetting the input
# buffer) on every poll.
_MAX_READS_PER_POLL = 40


class MovementGate:
    """Waits, battery-efficiently, for confirmed vehicle movement before a measuring run starts.

    Runs entirely on GNSS - the modem and quectel-CM are never touched here - polling at a low
    duty cycle rather than continuously, per decision record 0005. Movement only counts once it's
    sustained across consecutive polls, to tell genuine departure apart from GPS jitter or a
    brief roll; if a home position is configured, departure from it is required in addition to
    clearing the movement threshold.

    Also drives the one-time GNSS clock sync: GNSS is already being read here every poll, so each
    poll opportunistically checks one more line for a usable date/time until `clock_sync` reports
    synced, at which point that check stops.

    Every fix read here is recorded into the shared `gps_fix_status` tracker, so the heartbeat can
    report GPS status while this wait is still ongoing (see decision record 0021).
    """

    def __init__(
        self, gnss_receiver: GNSSReceiver, config: MovementGateConfig, run_phase: RunPhase,
        clock_sync: ClockSync, gps_fix_status: GpsFixStatusTracker,
    ):
        self._logger = logging.getLogger(__name__)
        self._gnss_receiver = gnss_receiver
        self._poll_interval_s = config.poll_interval_s
        self._movement_threshold = config.movement_threshold
        self._confirmations_required = config.confirmations_required
        self._home_departure_threshold = config.home_departure_threshold
        self._home_position = (
            Position(latitude=config.home_latitude, longitude=config.home_longitude)
            if config.home_latitude is not None and config.home_longitude is not None
            else None
        )
        self._run_phase = run_phase
        self._clock_sync = clock_sync
        self._gps_fix_status = gps_fix_status

    def wait_for_movement(self) -> None:
        """Blocks until sustained displacement (and, if configured, home departure) is confirmed."""
        self._run_phase.set(RunPhaseState.WAITING_FOR_GPS_FIX)
        self._logger.info("Waiting for a GPS fix...")

        reference: Position | None = None
        consecutive_confirmations = 0

        while True:
            fix = self._poll_fix()
            if fix is not None:
                if reference is None:
                    reference = fix.position
                    self._run_phase.set(RunPhaseState.WAITING_FOR_MOVEMENT)
                    self._logger.debug("Movement gate reference position set: %s", reference)
                elif self._has_departed(reference, fix.position):
                    consecutive_confirmations += 1
                    if consecutive_confirmations >= self._confirmations_required:
                        self._logger.info(
                            "Movement confirmed after %d consecutive polls.",
                            consecutive_confirmations,
                        )
                        return
                else:
                    consecutive_confirmations = 0
            time.sleep(self._poll_interval_s)

    def _poll_fix(self) -> GNSSFix | None:
        """Reads one fresh fix, reopening the receiver to discard sentences buffered since the last poll.

        Freshness comes entirely from the reopen (open() resets the input buffer) - the
        _MAX_READS_PER_POLL search below just bounds how long this looks for a GGA sentence among
        whatever else the receiver emits per cycle, so it doesn't need to be small to preserve
        that guarantee.

        While the clock hasn't been synced yet, also checks the next line for a usable GNSS
        date/time before closing - see the class docstring.
        """
        self._gnss_receiver.open()
        try:
            fix = None
            for _ in range(_MAX_READS_PER_POLL):
                fix = self._gnss_receiver.read_fix()
                if fix is not None:
                    self._gps_fix_status.record_fix(fix)
                    break
            self._try_clock_sync()
            return fix
        finally:
            self._gnss_receiver.close()

    def _try_clock_sync(self) -> None:
        """Reads one line for a GNSS date/time and syncs the clock from it, unless already synced."""
        if self._clock_sync.synced:
            return
        gnss_datetime = self._gnss_receiver.read_datetime()
        if gnss_datetime is not None:
            self._clock_sync.try_sync(gnss_datetime)

    def _has_departed(self, reference: Position, current: Position) -> bool:
        """Returns True if current position clears the movement threshold (and, if configured, home)."""
        if haversine_distance_m(reference, current) < self._movement_threshold:
            return False
        if self._home_position is not None:
            if haversine_distance_m(self._home_position, current) < self._home_departure_threshold:
                return False
        return True
