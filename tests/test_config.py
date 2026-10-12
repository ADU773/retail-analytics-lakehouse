from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from retail_lakehouse import resources
from retail_lakehouse.config import ConfigError, env_overrides, load_settings


def test_package_defaults() -> None:
    s = load_settings(environ={})
    assert s.generator.customers == 25_000
    assert s.generator.resolved_end() == date(2025, 12, 31)
    assert s.paths.warehouse_path == Path("data") / "warehouse" / "lakehouse.duckdb"
    assert s.as_of == date(2025, 12, 31)


def test_scale_preset_and_cli_overrides_win() -> None:
    s = load_settings(scale="tiny", environ={})
    assert s.generator.customers == 600
    s = load_settings(scale="tiny", overrides={"generator": {"customers": 999}}, environ={})
    assert s.generator.customers == 999


def test_environment_overrides_are_typed() -> None:
    env = {"RLH__GENERATOR__CUSTOMERS": "1234", "RLH__QUALITY__FAIL_ON_ERROR": "false"}
    assert env_overrides(env) == {
        "generator": {"customers": 1234},
        "quality": {"fail_on_error": False},
    }
    s = load_settings(environ=env)
    assert s.generator.customers == 1234
    assert s.quality.fail_on_error is False


def test_days_overrides_end() -> None:
    s = load_settings(overrides={"generator": {"start": "2024-03-01", "days": 90}}, environ={})
    assert s.generator.resolved_end() == date(2024, 5, 29)


def test_yaml_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "lakehouse.yaml"
    cfg.write_text("generator:\n  customers: 777\nresources:\n  workers: 3\n", encoding="utf-8")
    s = load_settings(cfg, environ={})
    assert s.generator.customers == 777
    assert s.resources.effective_workers() == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"generator": {"customers": 10}},
        {"generator": {"start": "2024-01-01", "end": "2024-01-10"}},
        {"generator": {"dq_issue_rate": 0.9}},
        {"generator": {"no_such_key": 1}},
        {"nonsense": {}},
    ],
)
def test_invalid_settings_raise(overrides: dict) -> None:
    with pytest.raises(ConfigError):
        load_settings(overrides=overrides, environ={})


def test_unknown_scale_and_missing_file() -> None:
    with pytest.raises(ConfigError):
        load_settings(scale="huge", environ={})
    with pytest.raises(ConfigError):
        load_settings("does-not-exist.yaml", environ={})


def test_resource_defaults_are_bounded() -> None:
    s = load_settings(environ={})
    limit = resources.default_memory_limit_bytes()
    assert 256 * resources.MIB <= limit <= 2 * resources.GIB
    assert s.resources.effective_memory_limit().endswith("MB")
    assert s.resources.effective_workers() == max(1, resources.physical_cores() - 1)
    assert s.resources.effective_threads() >= 1


def test_settings_to_dict_is_serialisable() -> None:
    import json

    d = load_settings(environ={}).to_dict()
    json.dumps(d)
    assert d["generator"]["start"] == "2024-01-01"
