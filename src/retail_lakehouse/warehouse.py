"""DuckDB connection management for the warehouse (silver/gold/meta) and helpers."""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

import duckdb

from retail_lakehouse.bronze.contracts import CONTRACTS, LINEAGE_COLUMNS
from retail_lakehouse.config import Settings
from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.sqlutil import sql_path, sql_str

log = get_logger(__name__)
SCHEMAS = ("bronze", "silver", "quarantine", "gold", "meta")


def sql_root(settings: Settings | None = None) -> Path:
    """Directory holding ``macros.sql`` and the ``silver/`` and ``gold/`` model folders."""
    if settings is not None and settings.paths.sql_dir is not None:
        return Path(settings.paths.sql_dir)
    return Path(str(resources.files("retail_lakehouse").joinpath("sql")))


def macros_sql(settings: Settings | None = None) -> str:
    """Text of the shared macro definitions."""
    return (sql_root(settings) / "macros.sql").read_text(encoding="utf-8")


def install_macros(con: duckdb.DuckDBPyConnection, settings: Settings | None = None) -> None:
    """Create (or replace) the shared SQL macros on ``con``."""
    con.execute(macros_sql(settings))


def connect(
    settings: Settings,
    path: Path | str | None = None,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection sized by the resource settings.

    ``path`` defaults to the warehouse file; pass ``":memory:"`` for a scratch connection.
    """
    target = str(path if path is not None else settings.paths.warehouse_path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    config = {
        "threads": settings.resources.effective_threads(),
        "memory_limit": settings.resources.effective_memory_limit(),
    }
    con = duckdb.connect(target, read_only=read_only, config=config)
    con.execute("SET TimeZone = 'UTC'")
    if not read_only:
        tmp = settings.paths.warehouse_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = {sql_path(tmp)}")
        for schema in SCHEMAS:
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        install_macros(con, settings)
    return con


def register_bronze_views(con: duckdb.DuckDBPyConnection, bronze_dir: Path) -> dict[str, int]:
    """(Re)create ``bronze.<entity>`` views over the partitioned Parquet files.

    Entities without files get an empty, correctly-typed view so downstream models still
    compile. Returns the number of Parquet files behind each view.
    """
    counts: dict[str, int] = {}
    for name, contract in CONTRACTS.items():
        entity_dir = bronze_dir / name
        files = list(entity_dir.rglob("*.parquet")) if entity_dir.exists() else []
        counts[name] = len(files)
        if files:
            glob = sql_str(f"{entity_dir.resolve().as_posix()}/**/*.parquet")
            con.execute(
                f"CREATE OR REPLACE VIEW bronze.{name} AS SELECT * FROM read_parquet("
                f"{glob}, hive_partitioning = true, union_by_name = true)"
            )
        else:
            cols = ", ".join(
                f"CAST(NULL AS VARCHAR) AS {c}" for c in (*contract.columns, *LINEAGE_COLUMNS)
            )
            con.execute(
                f"CREATE OR REPLACE VIEW bronze.{name} AS SELECT {cols}, "
                "CAST(NULL AS BIGINT) AS year, CAST(NULL AS BIGINT) AS month WHERE false"
            )
    return counts


def table_exists(con: duckdb.DuckDBPyConnection, qualified: str) -> bool:
    """True if ``schema.name`` exists as a table or view."""
    schema, name = qualified.split(".", 1)
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    return bool(row and row[0])


def compact(settings: Settings) -> tuple[int, int]:
    """Rewrite the warehouse file without free blocks; returns (bytes_before, bytes_after).

    DuckDB re-uses but never returns space freed by ``CREATE OR REPLACE TABLE``, so repeated
    rebuilds grow the file. ``COPY FROM DATABASE`` into a fresh file is the equivalent of a
    ``VACUUM FULL``. Requires that no other connection holds the warehouse open.
    """
    path = Path(settings.paths.warehouse_path)
    if not path.exists():
        return 0, 0
    before = path.stat().st_size
    tmp = path.with_name(path.stem + ".compact.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(
        ":memory:",
        config={
            "threads": settings.resources.effective_threads(),
            "memory_limit": settings.resources.effective_memory_limit(),
        },
    )
    try:
        con.execute(f"ATTACH {sql_path(path)} AS src (READ_ONLY)")
        con.execute(f"ATTACH {sql_path(tmp)} AS dst")
        con.execute("COPY FROM DATABASE src TO dst")
        con.execute("DETACH dst")
        con.execute("DETACH src")
    finally:
        con.close()
    os.replace(tmp, path)
    after = path.stat().st_size
    log.info("compacted warehouse: %.1f MB -> %.1f MB", before / 1e6, after / 1e6)
    return before, after
