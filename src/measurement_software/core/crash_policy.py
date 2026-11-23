import json
import logging
import traceback
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from measurement_software.core.util import utc_timestamp, write_json_atomically

logger = logging.getLogger(__name__)

CRASH_STATE_FILENAME = "crash_state.json"
CRASH_REPORT_FILENAME = "last_crash_report.json"

_PACKAGE_PREFIX = "measurement_software."


class CrashAction(StrEnum):
    REBOOT = "reboot"
    RECOVERY = "recovery"
    ENTER_MAINTENANCE = "enter_maintenance"


class _Escalation(StrEnum):
    RECOVERY = "recovery"
    RECOVERY_CONSUMED = "recovery_consumed"
    MAINTENANCE = "maintenance"


_LEGACY_ESCALATIONS = {"waiting": _Escalation.RECOVERY}


@dataclass(frozen=True)
class CrashDecision:
    """What to do about one unhandled error, and the counters that led there.

    `consecutive_reboots` counts the reboots for unhandled errors before this one.
    """

    action: CrashAction
    fingerprint: str
    same_error_count: int
    consecutive_reboots: int


@dataclass
class _CrashState:
    fingerprint: str | None = None
    same_error_count: int = 0
    consecutive_reboots: int = 0
    escalation: str | None = None


def error_fingerprint(error: BaseException) -> str:
    """Identifies an error by its type and where it was raised, as "Type@module:function".

    The location is the innermost traceback frame in this package (falling back to the innermost
    frame at all), so an error raised deep inside a library is still attributed to the code of
    ours that called it. The message is left out on purpose: it often carries details that vary
    between occurrences of the same fault (port names, counts, addresses).
    """
    location = "unknown"
    innermost_own = None
    tb = error.__traceback__
    while tb is not None:
        module = tb.tb_frame.f_globals.get("__name__", "")
        location = f"{module}:{tb.tb_frame.f_code.co_name}"
        if module.startswith(_PACKAGE_PREFIX):
            innermost_own = location
        tb = tb.tb_next
    return f"{type(error).__qualname__}@{innermost_own or location}"


class CrashPolicy:
    """Decides how the device recovers from an unhandled error, across reboots.

    An unknown error is often transient (a device dropping off USB, a brief hardware hiccup), so
    the first answer is a reboot. A reboot that only reproduces the error is a loop, though, so
    once the same error has occurred `same_error_limit` times in a row, or `reboot_limit`
    reboots for unhandled errors of any kind have happened in a row, the device escalates to the
    recovery state and reboots into it: that one boot doesn't measure until told to, so the
    fault can be looked at remotely. The recovery state is consumed (consume_recovery()) when
    the device enters it, so the boot after is a normal one. An error after that - or during
    the recovery boot itself - means the fault isn't in measuring alone: the next boot enters
    maintenance mode, which runs nothing but the cellular link and a critical-error heartbeat,
    so the device stays reachable for debugging over SSH instead of looping or switching itself
    off. The counters stay when the recovery state is consumed, so a consumed recovery can't be
    told apart from "never escalated" by them alone - the state therefore remembers it was used
    (otherwise the same thresholds would just trigger another recovery, forever).

    The state lives in a small file that survives reboots. A run that ends normally clears it
    (reset()); maintenance mode is only left by deleting that file and rebooting.
    """

    def __init__(self, state_dir: Path, same_error_limit: int, reboot_limit: int):
        self._state_path = state_dir / CRASH_STATE_FILENAME
        self._report_path = state_dir / CRASH_REPORT_FILENAME
        self._same_error_limit = same_error_limit
        self._reboot_limit = reboot_limit

    def record(self, error: BaseException) -> CrashDecision:
        """Counts `error` and returns how to recover from it."""
        state = self._load_state()
        fingerprint = error_fingerprint(error)
        same_error_count = state.same_error_count + 1 if state.fingerprint == fingerprint else 1

        if state.escalation is not None:
            action = CrashAction.ENTER_MAINTENANCE
            new_state = _CrashState(fingerprint, same_error_count, state.consecutive_reboots + 1, _Escalation.MAINTENANCE)
        elif same_error_count >= self._same_error_limit or state.consecutive_reboots >= self._reboot_limit:
            action = CrashAction.RECOVERY
            new_state = _CrashState(fingerprint, same_error_count, state.consecutive_reboots + 1, _Escalation.RECOVERY)
        else:
            action = CrashAction.REBOOT
            new_state = _CrashState(fingerprint, same_error_count, state.consecutive_reboots + 1)

        self._save_state(new_state)
        return CrashDecision(action, fingerprint, same_error_count, state.consecutive_reboots)

    def in_maintenance(self) -> bool:
        """Whether a previous boot decided this one has to run in maintenance mode."""
        return self._load_state().escalation == _Escalation.MAINTENANCE

    def in_recovery(self) -> bool:
        """Whether a previous boot decided this one is the one-shot recovery boot."""
        return self._load_state().escalation == _Escalation.RECOVERY

    def consume_recovery(self) -> None:
        """Marks the recovery state as used, so the next boot is a normal one.

        The counters stay; the escalation turns into "recovery consumed" rather than disappearing,
        so a crash on the following boot goes to maintenance mode instead of recovering again.
        """
        state = self._load_state()
        if state.escalation == _Escalation.RECOVERY:
            state.escalation = _Escalation.RECOVERY_CONSUMED
            self._save_state(state)

    def details(self) -> dict:
        """What recovery and maintenance mode report to the backend about why they're active."""
        state = self._load_state()
        return {
            "fingerprint": state.fingerprint,
            "same_error_count": state.same_error_count,
            "consecutive_reboots": state.consecutive_reboots,
        }

    def reset(self) -> None:
        """Clears the counters and any escalation, e.g. after a run that ended normally."""
        if self._state_path.exists():
            self._state_path.unlink()
            logger.info("Crash counters reset.")

    def write_report(self, decision: CrashDecision, error: BaseException) -> None:
        """Persists what happened, so the next boot can log it even if this boot's log is lost."""
        write_json_atomically(self._report_path, {
            "timestamp": utc_timestamp(),
            **asdict(decision),
            "traceback": "".join(traceback.format_exception(error)),
        })

    def log_previous_report(self) -> None:
        """Logs - and then removes - the report a previous boot left behind, if any."""
        if not self._report_path.exists():
            return
        try:
            report = json.loads(self._report_path.read_text())
            logger.warning(
                "Previous run ended with an unhandled error at %s (%s, same error %s time(s) in a "
                "row, %s reboot(s) in a row before it) - action: %s.\n%s",
                report.get("timestamp"), report.get("fingerprint"), report.get("same_error_count"),
                report.get("consecutive_reboots"), report.get("action"), report.get("traceback"),
            )
        except (OSError, ValueError) as e:
            logger.warning("Could not read the previous crash report %s: %s", self._report_path, e)
        self._report_path.unlink(missing_ok=True)

    def _load_state(self) -> _CrashState:
        if not self._state_path.exists():
            return _CrashState()
        try:
            state = _CrashState(**json.loads(self._state_path.read_text()))
            state.escalation = _LEGACY_ESCALATIONS.get(state.escalation, state.escalation)
            return state
        except (OSError, ValueError, TypeError) as e:
            logger.warning("Ignoring unreadable %s: %s", self._state_path.name, e)
            return _CrashState()

    def _save_state(self, state: _CrashState) -> None:
        write_json_atomically(self._state_path, asdict(state))
