import logging
import time
from typing import Callable

from measurement_software.core.run_phase import RunPhase, RunPhaseState
from measurement_software.core.system import ClockSync
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver
from measurement_software.status.backend_status import GpsFixStatusTracker

# GNSS date/time arrives in ZDA sentences, about one per report cycle, so a single read right
# after a fix almost never lands on one. This bounds how many lines the clock sync looks through.
MAX_READS_FOR_CLOCK_SYNC = 40


class FirstFixWaiter:
    """Blocks, in the WAITING_FOR_GPS_FIX phase, until the receiver reports a fix.

    The one place the device waits for a first fix, on every path into measuring. Keeps the
    receiver open for the whole wait and closes it again before returning, so whoever opens it next
    (`MovementGate`, `Collector.collect()`) finds the serial port free.

    Every fix and diagnostic read is recorded into the shared `gps_fix_status` tracker, so the
    heartbeat reports GPS status during the wait. Once a fix exists, the GNSS date/time is used to
    sync the clock (`ClockSync` keeps its own plausibility checks).
    """

    def __init__(
        self, gnss_receiver: GNSSReceiver, wait_log_interval_s: float, run_phase: RunPhase,
        clock_sync: ClockSync, gps_fix_status: GpsFixStatusTracker,
    ):
        self._logger = logging.getLogger(__name__)
        self._gnss_receiver = gnss_receiver
        self._wait_log_interval_s = wait_log_interval_s
        self._run_phase = run_phase
        self._clock_sync = clock_sync
        self._gps_fix_status = gps_fix_status

    def wait(self, check_remote_commands: Callable[[], None], timeout_s: float | None) -> GNSSFix | None:
        """Returns the first fix, or None if `timeout_s` passes first (None: never times out).

        `check_remote_commands` runs once per iteration, before the receiver is read, and ends
        the wait by raising - shutdown-now, reboot-now and stop-measuring-now take effect
        however long the fix takes.
        """
        self._run_phase.set(RunPhaseState.WAITING_FOR_GPS_FIX)
        start = time.time()
        last_wait_log = 0.0

        self._gnss_receiver.open()
        try:
            while True:
                check_remote_commands()

                now = time.time()
                elapsed = now - start
                if timeout_s is not None and elapsed > timeout_s:
                    self._logger.info("No GPS fix in specified interval of %s s. - Aborting.", timeout_s)
                    return None
                if (now - last_wait_log) >= self._wait_log_interval_s:
                    self._logger.info("Waiting for first GPS fix… (for %s s by now)", int(elapsed))
                    last_wait_log = now

                fix = self._read_fix()
                if fix is not None:
                    self._sync_clock()
                    return fix
        finally:
            self._gnss_receiver.close()

    def _read_fix(self) -> GNSSFix | None:
        fix = self._gnss_receiver.read_fix()
        if fix is not None:
            self._gps_fix_status.record_fix(fix)
        diagnostic = self._gnss_receiver.read_gnss_diagnostic()
        if diagnostic is not None:
            self._gps_fix_status.record_diagnostic(diagnostic)
        return fix

    def _sync_clock(self) -> None:
        for _ in range(MAX_READS_FOR_CLOCK_SYNC):
            if self._clock_sync.synced:
                return
            gnss_datetime = self._gnss_receiver.read_datetime()
            if gnss_datetime is not None:
                self._clock_sync.try_sync(gnss_datetime)
