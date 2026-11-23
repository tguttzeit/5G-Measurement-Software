import dataclasses
import tomllib
from pathlib import Path

from measurement_software.core.config import AppConfig

SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config.toml"

# Shipped values that are placeholders or per-device examples, not defaults.
INTENTIONALLY_DIFFERENT = {
    ("backend", "url"),
    ("logging", "log_file"),
    ("quectel_cm", "args"),
}


def _section_classes() -> dict[str, type]:
    return {f.name: f.type for f in dataclasses.fields(AppConfig)}


def _default_of(field: dataclasses.Field) -> object:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:
        return field.default_factory()
    return dataclasses.MISSING


def _as_toml_value(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def _drifted_fields() -> list[str]:
    with open(SHIPPED_CONFIG, "rb") as f:
        shipped = tomllib.load(f)
    section_classes = _section_classes()
    drifted = []
    for section, values in shipped.items():
        fields = {f.name: f for f in dataclasses.fields(section_classes[section])}
        for key, shipped_value in values.items():
            if (section, key) in INTENTIONALLY_DIFFERENT:
                continue
            default = _default_of(fields[key])
            if default is dataclasses.MISSING:
                continue
            if _as_toml_value(default) != shipped_value:
                drifted.append(f"{section}.{key}: default {default!r}, config.toml {shipped_value!r}")
    return drifted


def test_dataclass_defaults_match_the_shipped_config_toml():
    assert _drifted_fields() == []


def test_intentional_differences_still_name_existing_fields():
    section_classes = _section_classes()
    for section, key in INTENTIONALLY_DIFFERENT:
        assert key in {f.name for f in dataclasses.fields(section_classes[section])}
