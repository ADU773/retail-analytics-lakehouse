from __future__ import annotations

import duckdb
import pytest

from retail_lakehouse.bronze.contracts import CONTRACTS, LINEAGE_COLUMNS
from retail_lakehouse.bronze.ingest import IngestError, ingest
from retail_lakehouse.generator.runner import generate


def test_ingest_is_idempotent_and_partitioned(small_settings) -> None:
    gen = generate(small_settings, workers=1)
    first = ingest(small_settings)
    assert first.new_files == gen.files
    assert first.rows == gen.total_rows

    second = ingest(small_settings)
    assert second.new_files == 0
    assert second.skipped_files == gen.files
    assert second.rows == 0

    bronze = small_settings.paths.bronze_dir
    parts = list((bronze / "orders").rglob("*.parquet"))
    assert parts and all("year=" in p.as_posix() and "month=" in p.as_posix() for p in parts)

    con = duckdb.connect()
    glob = (bronze / "orders").as_posix() + "/**/*.parquet"
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{glob}')").fetchall()]
    assert set(LINEAGE_COLUMNS) <= set(cols)
    assert set(CONTRACTS["orders"].columns) <= set(cols)
    n, batches = con.execute(
        f"SELECT count(*), count(DISTINCT _batch_id) FROM read_parquet('{glob}')"
    ).fetchone()
    assert n == gen.rows["orders"] and batches > 1
    meta = con.execute(
        f"SELECT compression FROM parquet_metadata('{parts[0].as_posix()}') LIMIT 1"
    ).fetchone()
    assert meta[0].upper() == "ZSTD"
    con.close()


def test_changed_landing_file_is_rejected(small_settings) -> None:
    generate(small_settings, workers=1)
    ingest(small_settings)
    victim = next((small_settings.paths.raw_dir / "products").rglob("*.csv"))
    lines = victim.read_text(encoding="utf-8").splitlines(keepends=True)
    victim.write_text("".join([*lines, lines[-1]]), encoding="utf-8")  # re-delivered row
    with pytest.raises(IngestError, match="changed after it was ingested"):
        ingest(small_settings)
    assert ingest(small_settings, full_refresh=True).new_files > 0


def test_missing_landing_zone(small_settings) -> None:
    with pytest.raises(IngestError):
        ingest(small_settings)
