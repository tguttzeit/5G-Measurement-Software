import logging
import threading

from measurement_software.core import system
from measurement_software.core.config import FanConfig


class FanController:
    """Drives a case fan on/off via GPIO based on CPU temperature, with two-threshold hysteresis.

    Runs on its own background thread for the whole process lifetime, independent of
    Collector's measurement state — the CPU can overheat regardless of whether a measurement
    session is actively running or idle.
    """

    def __init__(self, config: FanConfig):
        self._logger = logging.getLogger(__name__)
        self._config = config
        self._fan_on = False
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Starts the background fan-control thread, unless fan control is disabled."""
        if not self._config.enabled:
            self._logger.info("Fan control is disabled.")
            return
        system.setup_fan_gpio(self._config.gpio_pin)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signals the fan-control thread to stop and waits for it to exit."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run(self, stop_event: threading.Event) -> None:
        """Applies hysteresis on the configured poll interval until stopped."""
        while True:
            self._update()
            if stop_event.wait(self._config.poll_interval_s):
                return

    def _update(self) -> None:
        """Reads the CPU temperature and toggles the fan if a threshold is crossed."""
        try:
            temperature = system.read_cpu_temperature_celsius()
        except OSError as e:
            self._logger.warning("Could not read CPU temperature: %s", e)
            return

        if not self._fan_on and temperature >= self._config.temp_on_celsius:
            self._set_fan(True)
        elif self._fan_on and temperature <= self._config.temp_off_celsius:
            self._set_fan(False)

    def _set_fan(self, on: bool) -> None:
        self._fan_on = on
        system.set_fan_state(self._config.gpio_pin, on)
        self._logger.info("Fan turned %s (CPU temperature threshold crossed)", "on" if on else "off")
