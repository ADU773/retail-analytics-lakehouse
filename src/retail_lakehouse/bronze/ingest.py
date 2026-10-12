"""Bronze ingestion: raw CSV / JSON Lines -> Hive-partitioned, zstd-compressed Parquet.

* Bronze keeps every source column as text (raw fidelity) and adds lineage columns
  ``_ingested_at``, ``_source_file``, ``_batch_id`` and ``_run_id``.
* Files are partitioned by ``year=/month=`` of the entity's event timestamp (parsed leniently;
  unparseable values fall back to the batch month), so late-arriving records land in the
  partition of the period they describe.
* Idempotent: the ingest manifest records each raw file's SHA-256. Re-runs skip known files,
  and output is written to a staging folder and moved into place before the manifest is
  updated, so a crash never leaves half-registered data behind.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from retail_lakehouse.bronze.contracts import CONTRACTS, EntityContract
from retail_lakehouse.bronze.manifest import (
    STATE_FILE,
    IngestManifest,
    ManifestEntry,
    file_sha256,
)
from retail_lakehouse.config import Settings
from retail_lakehouse.generator.runner import MANIFEST_NAME
from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.sqlutil import sql_path
from retail_lakehouse.warehouse import connect

log = get_logger(__name__)


class IngestError(RuntimeError):
    """Raised when the landing zone violates its contract."""


@dataclass
class EntityIngest:
    """Per-entity ingestion statistics."""

    entity: str
    new_files: int = 0
    skipped_files: int = 0
    rows: int = 0
    seconds: float = 0.0
    extra_columns: list[str] = field(default_factory=list)


@dataclass
class IngestSummary:
    """Result of an ingestion run."""

    run_id: str
    entities: dict[str, EntityIngest]
    seconds: float
    full_refresh: bool

    @property
    def new_files(self) -> int:
        return sum(e.new_files for e in self.entities.values())

    @property
    def skipped_files(self) -> int:
        return sum(e.skipped_files for e in self.entities.values())

    @property
    def rows(self) -> int:
        return sum(e.rows for e in self.entities.values())


def _quote_list(paths: list[Path]) -> str:
    return "[" + ", ".join(sql_path(p) for p in paths) + "]"


def _reader_sql(contract: EntityContract, files: list[Path]) -> str:
    if contract.fmt == "csv":
        return (
            f"read_csv({_quote_list(files)}, header = true, all_varchar = true, "
            "filename = true, union_by_name = true, quote = '\"', escape = '\"')"
        )
    cols = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in contract.columns) + "}"
    return (
        f"read_json({_quote_list(files)}, format = 'newline_delimited', columns = {cols}, "
        "filename = true)"
    )


def _check_schema(con: duckdb.DuckDBPyConnection, contract: EntityContract) -> list[str]:
    cols = [r[0] for r in con.execute("DESCRIBE stage").fetchall()]
    present = set(cols) - {"filename"}
    missing = [c for c in contract.columns if c not in present]
    if missing:
        raise IngestError(
            f"{contract.name}: raw files are missing contract column(s) {missing}; "
            "fix the export or update bronze/contracts.py"
        )
    extra = sorted(present - set(contract.columns))
    if extra:
        log.warning("%s: schema drift - unexpected column(s) kept as-is: %s", contract.name, extra)
    return extra


def _ingest_entity(
    con: duckdb.DuckDBPyConnection,
    contract: EntityContract,
    new_files: list[tuple[Path, str, str]],
    bronze_dir: Path,
    run_id: str,
    ingested_at: datetime,
) -> tuple[list[ManifestEntry], int, list[str]]:
    paths = [p for p, _, _ in new_files]
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE stage AS SELECT * FROM {_reader_sql(contract, paths)}"
    )
    extra = _check_schema(con, contract)
    norm = "replace(filename, '\\', '/')"
    ts = ingested_at.strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE stage_lineage AS
        SELECT * EXCLUDE (filename),
               TIMESTAMP '{ts}' AS _ingested_at,
               regexp_extract({norm}, '(raw/[^/]+/[^/]+/[^/]+)$', 1) AS _source_file,
               regexp_extract({norm}, '/([^/]+)/[^/]+$', 1) AS _batch_id,
               '{run_id}' AS _run_id,
               coalesce(parse_ts({contract.partition_column}),
                        CAST(strptime(regexp_extract({norm}, '/([^/]+)/[^/]+$', 1) || '-01',
                                      '%Y-%m-%d') AS TIMESTAMP)) AS _partition_ts
        FROM stage
        """
    )
    counts = dict(
        con.execute("SELECT _source_file, count(*) FROM stage_lineage GROUP BY 1").fetchall()
    )
    staging = bronze_dir / "_staging" / run_id / contract.name
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"""
        COPY (
            SELECT * EXCLUDE (_partition_ts),
                   year(_partition_ts) AS year, month(_partition_ts) AS month
            FROM stage_lineage
        ) TO {sql_path(staging)}
        (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (year, month),
         FILENAME_PATTERN 'part-{run_id}-{{i}}', OVERWRITE_OR_IGNORE true)
        """
    )
    target = bronze_dir / contract.name
    for src in staging.rglob("*.parquet"):
        dest = target / src.relative_to(staging)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dest)
    shutil.rmtree(staging, ignore_errors=True)
    con.execute("DROP TABLE IF EXISTS stage")
    con.execute("DROP TABLE IF EXISTS stage_lineage")

    iso = ingested_at.isoformat(timespec="seconds")
    entries = [
        ManifestEntry(
            source_file=rel,
            entity=contract.name,
            batch_id=Path(rel).parts[2],
            sha256=sha,
            bytes=path.stat().st_size,
            rows=int(counts.get(rel, 0)),
            run_id=run_id,
            ingested_at=iso,
        )
        for path, rel, sha in new_files
    ]
    return entries, sum(int(v) for v in counts.values()), extra


def raw_fingerprint(raw_dir: Path) -> str | None:
    """Fingerprint of the generated dataset currently in the landing zone (if known)."""
    manifest = raw_dir / MANIFEST_NAME
    if not manifest.exists():
        return None
    return json.loads(manifest.read_text(encoding="utf-8")).get("fingerprint")


def ingest(
    settings: Settings,
    full_refresh: bool = False,
    refresh_on_new_dataset: bool = False,
) -> IngestSummary:
    """Ingest new raw files into bronze Parquet.

    Args:
        settings: resolved settings.
        full_refresh: delete bronze and rebuild it from every raw file.
        refresh_on_new_dataset: full refresh automatically when the landing zone holds a
            different generated dataset (fingerprint) than the one bronze was built from.
    """
    t0 = time.perf_counter()
    paths = settings.paths
    raw_dir, bronze_dir = paths.raw_dir, paths.bronze_dir
    if not raw_dir.exists():
        raise IngestError(f"landing zone {raw_dir} does not exist - run `generate` first")

    state_path = bronze_dir / STATE_FILE
    fingerprint = raw_fingerprint(raw_dir)
    if refresh_on_new_dataset and state_path.exists() and fingerprint:
        previous = json.loads(state_path.read_text(encoding="utf-8")).get("fingerprint")
        if previous != fingerprint:
            log.info(
                "landing zone holds a new dataset (%s != %s): full refresh", fingerprint, previous
            )
            full_refresh = True
    if full_refresh and bronze_dir.exists():
        shutil.rmtree(bronze_dir)
    bronze_dir.mkdir(parents=True, exist_ok=True)

    manifest = IngestManifest(bronze_dir)
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    run_id = now.strftime("%Y%m%dT%H%M%S")
    con = connect(settings, ":memory:")
    entities: dict[str, EntityIngest] = {}
    try:
        for name, contract in CONTRACTS.items():
            te = time.perf_counter()
            stats = EntityIngest(name)
            files = sorted((raw_dir / name).glob(f"*/*.{contract.extension}"))
            new: list[tuple[Path, str, str]] = []
            for f in files:
                rel = f.relative_to(paths.data_dir).as_posix()
                sha = file_sha256(f)
                status = manifest.status(rel, sha)
                if status == "ingested":
                    stats.skipped_files += 1
                elif status == "changed":
                    raise IngestError(
                        f"{rel} changed after it was ingested (landing files are immutable). "
                        "Re-run with --full-refresh to rebuild bronze."
                    )
                else:
                    new.append((f, rel, sha))
            if new:
                entries, rows, extra = _ingest_entity(con, contract, new, bronze_dir, run_id, now)
                manifest.append(entries)
                stats.new_files, stats.rows, stats.extra_columns = len(new), rows, extra
            stats.seconds = time.perf_counter() - te
            entities[name] = stats
            log.info(
                "bronze.%-15s new files=%3d skipped=%3d rows=%s (%.2fs)",
                name,
                stats.new_files,
                stats.skipped_files,
                f"{stats.rows:,}",
                stats.seconds,
            )
    finally:
        con.close()
    shutil.rmtree(bronze_dir / "_staging", ignore_errors=True)
    state_path.write_text(
        json.dumps({"fingerprint": fingerprint, "last_run_id": run_id}, indent=2), encoding="utf-8"
    )
    summary = IngestSummary(run_id, entities, time.perf_counter() - t0, full_refresh)
    log.info(
        "ingest finished: %d new file(s), %d skipped, %s rows in %.1fs",
        summary.new_files,
        summary.skipped_files,
        f"{summary.rows:,}",
        summary.seconds,
    )
    return summary
