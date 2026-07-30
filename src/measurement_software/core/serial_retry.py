import logging
import time
from typing import Callable, TypeVar

import serial

T = TypeVar("T")

DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY_S = 0.75


def read_with_retry(
    read: Callable[[], T],
    is_transient: Callable[[T], bool],
    *,
    retries: int = DEFAULT_RETRIES,
    delay_s: float = DEFAULT_RETRY_DELAY_S,
    logger: logging.Logger | None = None,
) -> T:
    """Runs `read`, retrying transient serial failures up to `retries` times, `delay_s` apart.

    A `serial.SerialTimeoutException`, or a result `is_transient` flags as a short/empty read
    (e.g. vehicle vibration momentarily jostling the connector), is treated as recoverable and
    retried. Any other `serial.SerialException` means the port itself is gone (device removed,
    permission denied, I/O error on disconnect) — retrying can't fix that, so it propagates
    immediately instead of spending the retry budget on it.
    """
    logger = logger or logging.getLogger(__name__)
    last_timeout: serial.SerialTimeoutException | None = None

    for attempt in range(1, retries + 1):
        try:
            result = read()
        except serial.SerialTimeoutException as e:
            logger.warning("Transient serial timeout (attempt %d/%d): %s", attempt, retries, e)
            last_timeout = e
        except serial.SerialException:
            raise
        else:
            if not is_transient(result):
                return result
            logger.warning("Short/empty serial read (attempt %d/%d)", attempt, retries)
            last_timeout = None

        if attempt < retries:
            time.sleep(delay_s)

    if last_timeout is not None:
        raise last_timeout
    return result
