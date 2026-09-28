from pathlib import Path

import pytest

from measurement_software import promote_overrides
from measurement_software.core.config_sources import read_overrides_file, write_overrides_file


def write_minimal_config_toml(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("[collector]\nmax_idle_time = 200\n")
    return path


class TestParseArgs:
    def test_takes_no_positional_arguments(self):
        args = promote_overrides.parse_args([])

        assert vars(args) == {}

    def test_rejects_an_unexpected_argument(self):
        with pytest.raises(SystemExit):
            promote_overrides.parse_args(["idle_threshold_s"])


class TestMain:
    def test_promotes_and_reports_what_changed(self, monkeypatch, tmp_path, capsys):
        config_path = write_minimal_config_toml(tmp_path)
        write_overrides_file(tmp_path / "config_overrides.toml", {"idle_threshold_s": 500})
        monkeypatch.setattr(promote_overrides, "config_toml_path", lambda: config_path)

        exit_code = promote_overrides.main([])

        assert exit_code == 0
        assert "idle_threshold_s" in capsys.readouterr().out
        assert "max_idle_time = 500" in config_path.read_text()
        assert read_overrides_file(tmp_path / "config_overrides.toml") == {}

    def test_reports_and_succeeds_when_nothing_is_set(self, monkeypatch, tmp_path, capsys):
        config_path = write_minimal_config_toml(tmp_path)
        monkeypatch.setattr(promote_overrides, "config_toml_path", lambda: config_path)

        exit_code = promote_overrides.main([])

        assert exit_code == 0
        assert "Nothing to promote" in capsys.readouterr().out
