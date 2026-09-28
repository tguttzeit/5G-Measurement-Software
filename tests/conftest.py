import subprocess

import pytest

_RealPopen = subprocess.Popen


class _NoSudoPopen(_RealPopen):
    """Refuses to start any `sudo` command - the tests run on real devices too, where `sudo` works
    without a password and e.g. `sudo shutdown -r now` would reboot the machine running them."""

    def __init__(self, args, *popen_args, **popen_kwargs):
        if isinstance(args, (list, tuple)) and args and args[0] == "sudo":
            raise AssertionError(f"A test tried to run a real sudo command: {list(args)}")
        super().__init__(args, *popen_args, **popen_kwargs)


@pytest.fixture(autouse=True)
def no_real_sudo_commands(monkeypatch):
    """Safety net under the per-test subprocess fakes, which replace this one where they're used."""
    monkeypatch.setattr(subprocess, "Popen", _NoSudoPopen)
