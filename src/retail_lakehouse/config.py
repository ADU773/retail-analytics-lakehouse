"""Layered configuration: package defaults -> YAML file -> environment -> CLI flags.

Environment overrides use the ``RLH__<SECTION>__<KEY>`` convention, for example
``RLH__GENERATOR__CUSTOMERS=5000`` or ``RLH__RESOURCES__DUCKDB_THREADS=4``. Values are parsed
with YAML so numbers, booleans and dates get their natural types.
"""

from __future__ import annotations

import copy
import dataclasses
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from retail_lakehouse import resources as sysres

ENV_PREFIX = "RLH__"
CONFIG_ENV_VAR = "RLH_CONFIG"
PROJECT_CONFIG_NAME = "lakehouse.yaml"


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


@dataclass
class PathsConfig:
    """Filesystem locations (relative paths resolve against the working directory)."""

    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")
    docs_dir: Path = Path("docs")
    readme: Path = Path("README.md")
    sql_dir: Path | None = None
    dq_dir: Path | None = None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def bronze_dir(self) -> Path:
        return self.data_dir / "bronze"

    @property
    def warehouse_dir(self) -> Path:
        return self.data_dir / "warehouse"

    @property
    def warehouse_path(self) -> Path:
        return self.warehouse_dir / "lakehouse.duckdb"

    @property
    def dq_reports_dir(self) -> Path:
        return self.reports_dir / "dq"


@dataclass
class GeneratorConfig:
    """Synthetic data generator knobs."""

    seed: int = 42
    customers: int = 25_000
    start: date = date(2024, 1, 1)
    end: date = date(2025, 12, 31)
    days: int | None = None
    dq_issue_rate: float = 0.015
    late_arrival_rate: float = 0.01
    anonymous_sessions_per_customer: float = 5.0

    def resolved_end(self) -> date:
        """Return the effective end date (``days`` wins over ``end`` when set)."""
        if self.days is not None:
            return self.start + timedelta(days=int(self.days) - 1)
        return self.end


@dataclass
class ResourceConfig:
    """CPU / memory sizing. ``None`` means "derive from the machine via psutil"."""

    workers: int | None = None
    duckdb_threads: int | None = None
    duckdb_memory_limit: str | None = None

    def effective_workers(self) -> int:
        return int(self.workers) if self.workers else sysres.default_workers()

    def effective_threads(self) -> int:
        return int(self.duckdb_threads) if self.duckdb_threads else sysres.default_duckdb_threads()

    def effective_memory_limit(self) -> str:
        if self.duckdb_memory_limit:
            return str(self.duckdb_memory_limit)
        return sysres.format_duckdb_memory(sysres.default_memory_limit_bytes())


@dataclass
class QualityConfig:
    """Data-quality framework behaviour."""

    fail_on_error: bool = True
    as_of: date | None = None


@dataclass
class ReportConfig:
    """Static report options."""

    top_pairs: int = 15
    images: bool = True


@dataclass
class Settings:
    """Fully resolved settings object passed around the pipeline."""

    paths: PathsConfig = field(default_factory=PathsConfig)
    generator: GeneratorConfig = field(default_factory=GeneratorConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    scales: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def as_of(self) -> date:
        """Reference date for freshness checks and RFM recency."""
        return self.quality.as_of or self.generator.resolved_end()

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view (paths and dates rendered as strings)."""

        def _conv(value: Any) -> Any:
            if isinstance(value, Path):
                return value.as_posix()
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: _conv(v) for k, v in value.items()}
            return value

        return {
            section: _conv(dataclasses.asdict(getattr(self, section)))
            for section in ("paths", "generator", "resources", "quality", "report")
        }


_SECTIONS: dict[str, type] = {
    "paths": PathsConfig,
    "generator": GeneratorConfig,
    "resources": ResourceConfig,
    "quality": QualityConfig,
    "report": ReportConfig,
}


def load_default_dict() -> dict[str, Any]:
    """Load the defaults shipped inside the package."""
    text = resources.files("retail_lakehouse").joinpath("defaults/default.yaml").read_text("utf-8")
    return yaml.safe_load(text) or {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect ``RLH__SECTION__KEY=value`` environment variables into a nested dict."""
    environ = dict(os.environ) if environ is None else environ
    out: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in name[len(ENV_PREFIX) :].split("__") if p]
        if len(parts) != 2:
            continue
        section, key = parts
        out.setdefault(section, {})[key] = yaml.safe_load(raw) if raw != "" else None
    return out


def _coerce(value: Any, annotation: Any, where: str) -> Any:
    """Coerce a YAML/env/CLI value to the dataclass field type."""
    ann = str(annotation)
    if value is None or (isinstance(value, str) and value.lower() in {"auto", "null", "none", ""}):
        if "None" in ann:
            return None
        raise ConfigError(f"{where} may not be empty")
    try:
        if "Path" in ann:
            return Path(str(value))
        if "date" in ann:
            return value if isinstance(value, date) else date.fromisoformat(str(value))
        if "bool" in ann:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if "int" in ann and "float" not in ann:
            return int(value)
        if "float" in ann:
            return float(value)
        if "str" in ann:
            return str(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid value for {where}: {value!r} ({exc})") from exc
    return value


def _build_section(cls: type, values: dict[str, Any], section: str) -> Any:
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(values) - set(known)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    kwargs = {
        name: _coerce(val, known[name].type, f"{section}.{name}") for name, val in values.items()
    }
    return cls(**kwargs)


def build_settings(data: dict[str, Any]) -> Settings:
    """Create a :class:`Settings` object from a merged configuration dict."""
    unknown = set(data) - set(_SECTIONS) - {"scales"}
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {
        name: _build_section(cls, data.get(name) or {}, name) for name, cls in _SECTIONS.items()
    }
    settings = Settings(**kwargs, scales=data.get("scales") or {})
    validate(settings)
    return settings


def validate(settings: Settings) -> None:
    """Sanity-check settings and raise :class:`ConfigError` with a clear message."""
    gen = settings.generator
    if gen.customers < 50:
        raise ConfigError("generator.customers must be >= 50")
    end = gen.resolved_end()
    if end <= gen.start:
        raise ConfigError(f"generator end ({end}) must be after start ({gen.start})")
    if (end - gen.start).days < 59:
        raise ConfigError("the generated date range must cover at least 60 days")
    for name in ("dq_issue_rate", "late_arrival_rate"):
        rate = getattr(gen, name)
        if not 0.0 <= rate <= 0.2:
            raise ConfigError(f"generator.{name} must be between 0 and 0.2 (got {rate})")
    if settings.resources.workers is not None and settings.resources.workers < 1:
        raise ConfigError("resources.workers must be >= 1")


def load_settings(
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    scale: str | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Resolve settings from all layers.

    Precedence (lowest to highest): package defaults, ``--scale`` preset, YAML config file
    (``config_path``, ``$RLH_CONFIG`` or ``./lakehouse.yaml``), environment variables,
    explicit ``overrides`` (CLI flags).
    """
    data = load_default_dict()
    if scale:
        presets = data.get("scales") or {}
        if scale not in presets:
            raise ConfigError(f"unknown scale {scale!r}; choose from {sorted(presets)}")
        data = deep_merge(data, {"generator": presets[scale]})

    env = dict(os.environ) if environ is None else environ
    path = config_path or env.get(CONFIG_ENV_VAR)
    if path is None and Path(PROJECT_CONFIG_NAME).is_file():
        path = PROJECT_CONFIG_NAME
    if path is not None:
        cfg_file = Path(path)
        if not cfg_file.is_file():
            raise ConfigError(f"config file not found: {cfg_file}")
        file_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        if scale:  # an explicit --scale wins over generator sizes in the project file
            for key in ("customers", "start", "end", "days"):
                (file_data.get("generator") or {}).pop(key, None)
        data = deep_merge(data, file_data)

    data = deep_merge(data, env_overrides(env))
    data = deep_merge(data, overrides or {})
    return build_settings(data)
