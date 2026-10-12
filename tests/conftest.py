"""Shared fixtures: settings rooted in temp dirs and one tiny end-to-end pipeline run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from retail_lakehouse.config import Settings, deep_merge, load_settings
from retail_lakehouse.logging_setup import configure_logging

configure_logging("WARNING")


def make_settings(root: Path, **overrides: Any) -> Settings:
    """Settings whose data/reports/docs live under ``root`` (never the repo checkout).

    ``overrides`` are nested config sections, e.g. ``generator={"customers": 150}``.
    """
    base: dict[str, Any] = {
        "paths": {
            "data_dir": root / "data",
            "reports_dir": root / "reports",
            "docs_dir": root / "docs",
            "readme": root / "README.md",
        },
        "resources": {"workers": 2, "duckdb_threads": 2, "duckdb_memory_limit": "512MB"},
    }
    return load_settings(None, overrides=deep_merge(base, overrides), scale="tiny", environ={})


@pytest.fixture(scope="session")
def tiny_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """Settings for the shared tiny-scale lakehouse (600 customers, one year)."""
    return make_settings(tmp_path_factory.mktemp("tiny_lakehouse"))


@pytest.fixture(scope="session")
def tiny_run(tiny_settings: Settings):
    """Run the full pipeline once (generate -> ingest -> transform -> test -> report)."""
    from retail_lakehouse.pipeline import run_all

    return run_all(tiny_settings, workers=2)


@pytest.fixture
def small_settings(tmp_path: Path) -> Settings:
    """Very small, fast configuration for unit-level generator / ingest tests."""
    return make_settings(
        tmp_path,
        generator={"customers": 150, "start": "2024-01-01", "days": 75},
    )
