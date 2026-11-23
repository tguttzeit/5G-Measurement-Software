import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from measurement_software.core.capture_gate import create_capture_gate
from measurement_software.core.config import CollectorConfig, DeviceConfig
from measurement_software.core.datapoint import Datapoint
from measurement_software.core.modem_access import ModemAccess
from measurement_software.core.movement_tracker import MovementTracker
from measurement_software.core.run_log import RunLog
from measurement_software.core.system import KeepModemAliveSender
from measurement_software.core.util import haversine_distance_m, new_run_id, utc_timestamp
from measurement_software.gnss.gnss_receiver import GNSSFix, GNSSReceiver, Position
from measurement_software.modems.modem import Modem, ModemConnectionError
from measurement_software.status.backend_status import GpsFixStatusTracker, RunStatusTracker


_NO_FIX_PAUSE_S = 0.01


class RemoteShutdownRequested(Exception):
    """Raised to unwind an active collection run after a remote shutdown-now command.

    Carries the triggering command's id (None if the shutdown wasn't remote-triggered at all,
    e.g. an unhandled exception), so the unified shutdown sequence can ack it in its last
    heartbeat before the board loses power.
    """

    def __init__(self, command_id: Any | None = None):
        super().__init__()
        self.command_id = command_id


class RemoteRebootRequested(Exception):
    """Raised to unwind an active collection run after a remote reboot-now command.

    Carries the triggering command's id, same reasoning as RemoteShutdownRequested.
    """

    def __init__(self, command_id: Any | None = None):
        super().__init__()
        self.command_id = command_id


class StopMeasuringRequested(Exception):
    """Raised to end an active collection run after a remote stop-measuring-now command.

    Caught inside collect() itself, unlike RemoteShutdownRequested/RemoteRebootRequested - the
    run just ends early as if it had gone idle, taking the usual finalize/upload path rather
    than the emergency one those two take.
    """


class Collector:
    """Runs a GPS-triggered measurement session, sampling the modem per the configured capture gate."""

    def __init__(self, modem: Modem, gnss_receiver: GNSSReceiver, config: CollectorConfig,
                 run_status: RunStatusTracker, run_log: RunLog[Datapoint],
                 device: DeviceConfig, gps_fix_status: GpsFixStatusTracker | None = None,
                 run_id: str | None = None, on_sim_reset: Callable[[], None] | None = None,
                 idle_end_enabled: bool = True, modem_access: ModemAccess | None = None):
        self._logger = logging.getLogger(__name__)
        self._modem = modem
        self._modem_access = modem_access or ModemAccess(modem)
        self._gnss_receiver = gnss_receiver
        self._run_status = run_status
        self._run_log = run_log
        self._device_id = device.device_id
        self._mission_type = device.mission_type
        self._run_id = run_id or new_run_id()
        self._gps_fix_status = gps_fix_status
        self._on_sim_reset = on_sim_reset

        self._position_threshold = config.position_threshold
        self._idle_end_enabled = idle_end_enabled
        self._max_idle_time = config.max_idle_time
        self._gps_enabled = config.gps_enabled
        self._gps_disabled_poll_interval_s = config.gps_disabled_poll_interval_s
        self._capture_gate = create_capture_gate(config)
        self._modem_reconnect_attempts = config.modem_reconnect_attempts
        self._modem_reconnect_delay_s = config.modem_reconnect_delay_s

        self._keep_modem_alive = KeepModemAliveSender(
            config.keep_alive_host, config.keep_alive_port, config.keep_alive_interval_s
        )

        self._movement = MovementTracker()
        self._last_pos: Position | None = None
        self._last_movement_time: float | None = None

        self._remote_lock = threading.Lock()
        self._collecting = False
        self._shutdown_requested = False
        self._shutdown_command_id: Any | None = None
        self._reboot_requested = False
        self._reboot_command_id: Any | None = None
        self._stop_measuring_requested = False
        self._sim_reset_requested = False
        self._radio_reset_requested = False

    @property
    def run_id(self) -> str:
        """Identifies this run in everything it records, for anything recording alongside it."""
        return self._run_id

    @property
    def movement(self) -> MovementTracker:
        """Where the vehicle last moved, for anything running alongside the collection loop."""
        return self._movement

    @property
    def is_collecting(self) -> bool:
        """Whether collect() is currently running - the modem is open and the loop is live.

        Lets RemoteCommandDispatcher tell a genuinely inapplicable command (sim-reset-now with
        no modem connection open to reconnect, stop-measuring-now with no run to stop) apart
        from one that actually ran, rather than reporting both as "success".
        """
        with self._remote_lock:
            return self._collecting

    def request_shutdown(self, command_id: Any | None = None) -> None:
        """Asks the running collection loop to stop as soon as it next checks in.

        Thread-safe: called from the heartbeat thread when a remote shutdown-now command
        arrives, while the collection loop itself runs on a different thread. `command_id`
        (the triggering command's id) rides along on RemoteShutdownRequested for the final
        heartbeat's ack.
        """
        with self._remote_lock:
            self._shutdown_requested = True
            self._shutdown_command_id = command_id

    def request_reboot(self, command_id: Any | None = None) -> None:
        """Asks the running collection loop to stop for a reboot as soon as it next checks in.

        Thread-safe, same reasoning as request_shutdown().
        """
        with self._remote_lock:
            self._reboot_requested = True
            self._reboot_command_id = command_id

    def request_stop_measuring(self) -> None:
        """Asks the running collection loop to end the run (not the device) as soon as it next
        checks in.

        Thread-safe, same reasoning as request_shutdown(). Unlike request_shutdown()/
        request_reboot(), this ends only the run - the device finalizes, uploads, and proceeds
        exactly as it would after an ordinary idle timeout.
        """
        with self._remote_lock:
            self._stop_measuring_requested = True

    def request_sim_reset(self) -> None:
        """Reconnects the modem - immediately, from this thread, if collect() hasn't opened it
        yet; otherwise flags it for the collection thread to apply on its own next loop
        iteration.

        Before collect() opens the modem, nothing else ever touches it - movement_gate's and
        ManualModeLoop's own waits never go near self._modem - so acting immediately here is safe.
        Once collect() is running, the two phases would otherwise race on the same connection
        (the modem's serial connection isn't thread-safe), so it's deferred to request_shutdown()
        's same flag/check pattern instead.
        """
        with self._remote_lock:
            collecting = self._collecting
            if collecting:
                self._sim_reset_requested = True
        if not collecting:
            self._logger.warning("Remote sim-reset-now command received - reconnecting modem.")
            if self._on_sim_reset is not None:
                self._on_sim_reset()
            self._modem.close()
            self._modem.open()

    def request_radio_reset(self) -> bool:
        """Resets the modem's radio: on the collection thread's next loop iteration while
        collecting, otherwise right away from this thread through ModemAccess.

        Returns False, doing nothing, when the modem can't be used right now (see
        ModemAccess.reset_radio_if_idle()).
        """
        with self._remote_lock:
            if self._collecting:
                self._radio_reset_requested = True
                return True
        return self._modem_access.reset_radio_if_idle()

    def check_remote_shutdown_or_reboot(self) -> None:
        """Raises if a shutdown or reboot was requested - shutdown beats reboot if somehow both are.

        Unlike _check_remote_commands(), this concerns the whole device rather than an active
        measuring session, so it's safe (and meant) to be called from any phase - the
        pre-measurement GPS-fix/movement wait and Manual Mode's idle wait, not just collect()'s
        own loop - so shutdown-now/reboot-now take effect immediately regardless of what the
        device happens to be doing when they arrive.
        """
        with self._remote_lock:
            shutdown_requested = self._shutdown_requested
            shutdown_command_id = self._shutdown_command_id
            reboot_requested = self._reboot_requested
            reboot_command_id = self._reboot_command_id

        if shutdown_requested:
            self._logger.critical("Remote shutdown-now command received - ending collection.")
            raise RemoteShutdownRequested(shutdown_command_id)

        if reboot_requested:
            self._logger.critical("Remote reboot-now command received - ending collection.")
            raise RemoteRebootRequested(reboot_command_id)

    def check_remote_commands_before_measuring(self) -> None:
        """Raises if a shutdown, reboot or stop-measuring was requested; meant for the wait for the
        first GPS fix, before any run exists.

        Like check_remote_shutdown_or_reboot(), but also consumes a pending stop-measuring-now:
        the wait it interrupts ends with StopMeasuringRequested and the request is spent, so it
        cannot also end the next run.
        """
        self.check_remote_shutdown_or_reboot()
        with self._remote_lock:
            stop_measuring_requested = self._stop_measuring_requested
            self._stop_measuring_requested = False
        if stop_measuring_requested:
            self._logger.warning("Remote stop-measuring-now command received - ending the wait.")
            raise StopMeasuringRequested()

    def _check_remote_commands(self) -> None:
        """Applies a pending remote sim-reset in place; raises if a shutdown, reboot, or
        stop-measuring was requested.

        Called once per loop iteration on the collection thread only, so a modem reconnect
        never races the loop's own use of the modem.
        """
        with self._remote_lock:
            stop_measuring_requested = self._stop_measuring_requested
            self._stop_measuring_requested = False
            sim_reset_requested = self._sim_reset_requested
            self._sim_reset_requested = False
            radio_reset_requested = self._radio_reset_requested
            self._radio_reset_requested = False

        if radio_reset_requested:
            self._reset_radio()

        if sim_reset_requested:
            self._logger.warning("Remote sim-reset-now command received - reconnecting modem.")
            if self._on_sim_reset is not None:
                self._on_sim_reset()
            self._modem.close()
            self._modem.open()

        self.check_remote_shutdown_or_reboot()

        if stop_measuring_requested:
            self._logger.warning("Remote stop-measuring-now command received - ending collection.")
            raise StopMeasuringRequested()

    def collect(self) -> Path:
        """Records datapoints, starting with the first fix the receiver reports, until the run ends.

        The wait for a first fix happens before this, in its own phase (see FirstFixWaiter), so
        the fix this starts from is read fresh rather than carried over from that wait.

        Returns the run log written, which holds everything captured up to the moment the
        session ended - including a session ended by the power being cut.
        """
        with self._modem_access.claim():
            return self._collect()

    def _collect(self) -> Path:
        self._gnss_receiver.open()
        self._run_log.open()
        time.sleep(1)

        self._keep_modem_alive.start()
        self._logger.info("Starting data collection...")
        if not self._gps_enabled:
            self._logger.warning(
                "GPS disabled (config.collector.gps_enabled=false) — using placeholder fixes, "
                "not real GNSS hardware. Testing mode only."
            )

        with self._remote_lock:
            self._collecting = True
        try:
            self._record_until_idle()
        except StopMeasuringRequested:
            pass
        finally:
            with self._remote_lock:
                self._collecting = False
            self._keep_modem_alive.stop()
            self._run_log.close()
            self._gnss_receiver.close()
            self._logger.info("Finished data collection")

        return self._run_log.path

    def _record_until_idle(self) -> None:
        """Records a datapoint for every qualifying movement until the device sits still for too long.

        The idle end is checked after every read, fix or not: without a fix there is no evidence
        of movement, so the idle clock keeps running. It only starts once a first fix exists - the
        wait for that one is a phase of its own.
        """
        self._last_pos = None
        self._last_movement_time = None
        first_fix_seen = False

        for fix in self._iter_reads():
            if fix is not None:
                first_fix_seen = True
                self._track_genuine_movement(fix)
                if self._should_capture(fix):
                    self._run_log.append(self._capture_datapoints(fix))

            if first_fix_seen and self._has_been_idle_too_long():
                break

    def _iter_reads(self) -> Iterator[GNSSFix | None]:
        """Yields the result of every receiver read - None when no fix arrived - forever, applying
        pending remote commands after each read and before the result is used.

        Yielding None lets the caller keep its per-iteration checks running while no fix is arriving.
        With GPS disabled, the receiver has no natural read pace of its own (unlike blocking on
        real serial I/O), so this paces reads itself at gps_disabled_poll_interval_s.
        """
        while True:
            read_started = time.time()
            fix = self._read_fix()
            self._check_remote_commands()
            if fix is None:
                self._wait_if_read_returned_immediately(read_started)
            yield fix
            if not self._gps_enabled:
                time.sleep(self._gps_disabled_poll_interval_s)

    def _wait_if_read_returned_immediately(self, read_started: float) -> None:
        """Keeps a receiver that answers None without blocking from turning the loop into a busy loop.

        A serial read normally blocks up to its timeout, so a read that took longer than the pause
        gets none added; the pause is short enough to keep up with a receiver streaming NMEA lines.
        """
        if time.time() - read_started < _NO_FIX_PAUSE_S:
            time.sleep(_NO_FIX_PAUSE_S)

    def _read_fix(self) -> GNSSFix | None:
        """Reads one fix, recording it into the shared gps_fix_status tracker if one is wired.

        Also opportunistically reads one more line for a GNSS diagnostic message (e.g. an
        antenna fault) and records it into the same tracker - same reasoning as
        MovementGate._try_read_diagnostic(), so the fault is visible without needing a separate
        run-gps-fix-test-now session.
        """
        fix = self._gnss_receiver.read_fix()
        if fix is not None and self._gps_fix_status is not None:
            self._gps_fix_status.record_fix(fix)
        if self._gps_fix_status is not None:
            diagnostic = self._gnss_receiver.read_gnss_diagnostic()
            if diagnostic is not None:
                self._gps_fix_status.record_diagnostic(diagnostic)
        return fix

    def _should_capture(self, fix: GNSSFix) -> bool:
        """Decides whether to query the modem for this fix.

        With GPS disabled there's no real position to gate on — every paced fix captures
        unconditionally instead of going through the configured capture gate, exactly as before
        the gate became pluggable. Otherwise delegates to `self._capture_gate`
        (`config.collector.capture_gate` — distance- or time-based).
        """
        if not self._gps_enabled:
            return True
        return self._capture_gate.should_capture(fix)

    def _track_genuine_movement(self, fix: GNSSFix) -> None:
        """Updates the idle clock and `MovementTracker` for real vehicle movement on this fix.

        Runs regardless of which capture gate is configured: the idle clock that ends a run has
        to keep reflecting whether the vehicle has actually moved even
        under `capture_gate = "time"`, where capture decisions no longer do — otherwise a parked
        vehicle sampled on a fixed cadence would never go idle and the run would never end on its
        own. With GPS disabled there's no real position to detect movement from, so every paced
        fix is treated as movement instead, same as `_should_capture()`'s own GPS-disabled bypass.
        """
        if not self._gps_enabled:
            self._movement.record_movement(fix)
            self._last_movement_time = None
            return
        if self._has_moved_enough(fix):
            self._movement.record_movement(fix)

    def _has_moved_enough(self, fix: GNSSFix) -> bool:
        """Returns True and updates the reference position if the fix cleared the movement threshold.

        Genuine, distance-based movement detection used only to drive the idle clock and
        `MovementTracker` — independent of `self._capture_gate`, which may be deciding capture on
        a fixed time cadence instead. Resetting `_last_movement_time` here, not in the capture
        gate, is what keeps idle detection tied to real movement regardless of which gate is active.
        """
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
        """Queries the modem and pairs each cell sample with the given fix and current timestamp.

        If the connection to the modem is lost, this capture is skipped and the modem reopened.
        """
        timestamp = utc_timestamp()
        try:
            samples = self._modem.query_cell_info()
        except ModemConnectionError:
            self._logger.warning("Lost the connection to the modem - skipping this capture.", exc_info=True)
            self._reconnect_modem()
            return []
        self._run_status.record_capture(samples)

        datapoints = [
            Datapoint(
                timestamp=timestamp,
                device_id=self._device_id,
                mission_type=self._mission_type,
                run_id=self._run_id,
                fix=fix,
                cell_sample=sample,
            )
            for sample in samples
        ]
        for datapoint in datapoints:
            self._logger.debug("Datapoint captured: %s", datapoint)
        return datapoints

    def _reset_radio(self) -> None:
        self._logger.warning("Resetting the modem's radio (requested by the link watchdog).")
        try:
            self._modem.reset_radio()
        except ModemConnectionError:
            self._logger.warning("Lost the connection to the modem during the radio reset.", exc_info=True)
            self._reconnect_modem()

    def _reconnect_modem(self) -> None:
        """Reopens the modem, waiting between attempts for it to reappear on the bus.

        A modem that dropped off USB took about 10 s to re-enumerate in the field. Once every
        attempt has failed, the last ModemConnectionError propagates.
        """
        self._modem.close()
        for attempt in range(1, self._modem_reconnect_attempts + 1):
            time.sleep(self._modem_reconnect_delay_s)
            try:
                self._modem.open()
            except ModemConnectionError as e:
                self._logger.warning("Modem reconnect attempt %d/%d failed: %s",
                                     attempt, self._modem_reconnect_attempts, e)
                if attempt == self._modem_reconnect_attempts:
                    raise
            else:
                self._logger.info("Modem reconnected (attempt %d/%d).", attempt, self._modem_reconnect_attempts)
                return

    def _has_been_idle_too_long(self) -> bool:
        """Returns True once the device has gone without movement for longer than max_idle_time.

        Never True in Manual Mode, where the operator starts and ends the run by command and
        the device may sit still on purpose (a hovering drone). A max_idle_time of 0 also
        disables the idle end.
        """
        if not self._idle_end_enabled or self._max_idle_time == 0:
            return False

        if self._last_movement_time is None:
            self._last_movement_time = time.time()
            return False

        idle_duration = time.time() - self._last_movement_time
        if idle_duration >= self._max_idle_time:
            self._logger.info("No movement for %s s. Ending data collection.", self._max_idle_time)
            return True
        return False
