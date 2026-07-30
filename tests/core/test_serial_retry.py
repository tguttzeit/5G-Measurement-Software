import logging

import pytest
import serial

from measurement_software.core.serial_retry import read_with_retry


def is_empty(result: bytes) -> bool:
    return result == b""


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("measurement_software.core.serial_retry.time.sleep", lambda seconds: None)


def scripted(*results):
    """Returns a zero-arg callable that yields each of `results` in order on successive calls.

    An entry that is an Exception instance is raised instead of returned.
    """
    remaining = list(results)

    def read():
        item = remaining.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return read


class TestSuccess:
    def test_returns_result_immediately_when_not_transient(self):
        result = read_with_retry(scripted(b"data"), is_empty)
        assert result == b"data"

    def test_recovers_after_a_short_read_then_succeeds(self):
        result = read_with_retry(scripted(b"", b"data"), is_empty, retries=3)
        assert result == b"data"

    def test_recovers_after_a_timeout_exception_then_succeeds(self):
        result = read_with_retry(
            scripted(serial.SerialTimeoutException("write timed out"), b"data"), is_empty, retries=3
        )
        assert result == b"data"


class TestExhaustedRetries:
    def test_returns_last_short_read_once_budget_is_exhausted(self):
        result = read_with_retry(scripted(b"", b"", b""), is_empty, retries=3)
        assert result == b""

    def test_raises_last_timeout_once_budget_is_exhausted(self):
        error = serial.SerialTimeoutException("write timed out")
        with pytest.raises(serial.SerialTimeoutException):
            read_with_retry(scripted(error, error, error), is_empty, retries=3)

    def test_stops_after_configured_number_of_attempts(self):
        calls = []

        def read():
            calls.append(1)
            return b""

        read_with_retry(read, is_empty, retries=3)
        assert len(calls) == 3


class TestFaultPropagatesImmediately:
    def test_serial_exception_is_not_retried(self):
        calls = []

        def read():
            calls.append(1)
            raise serial.SerialException("device disconnected")

        with pytest.raises(serial.SerialException):
            read_with_retry(read, is_empty, retries=3)

        assert len(calls) == 1

    def test_other_exceptions_are_not_caught(self):
        def read():
            raise ValueError("not a serial error")

        with pytest.raises(ValueError):
            read_with_retry(read, is_empty, retries=3)


class TestLogging:
    def test_logs_warning_for_short_read(self, caplog):
        caplog.set_level(logging.WARNING)
        read_with_retry(scripted(b"", b"data"), is_empty, retries=3)
        assert "Short/empty serial read" in caplog.text

    def test_logs_warning_for_transient_timeout(self, caplog):
        caplog.set_level(logging.WARNING)
        read_with_retry(
            scripted(serial.SerialTimeoutException("timed out"), b"data"), is_empty, retries=3
        )
        assert "Transient serial timeout" in caplog.text
