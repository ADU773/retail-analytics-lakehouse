"""Writers for the raw landing zone: ``data/raw/<entity>/<batch>/<file>``."""

from __future__ import annotations

import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

from retail_lakehouse.sqlutil import sql_path

Columns = dict[str, np.ndarray]
_CON: duckdb.DuckDBPyConnection | None = None


def _duckdb() -> duckdb.DuckDBPyConnection:
    """One small in-memory DuckDB per (worker) process, used as a fast CSV writer."""
    global _CON
    if _CON is None:
        _CON = duckdb.connect(config={"threads": 1})
    return _CON


def _to_arrow(values: np.ndarray) -> pa.Array:
    if values.dtype == object:
        return pa.array([None if v is None else str(v) for v in values.tolist()], type=pa.string())
    if values.dtype.kind == "b":
        return pa.array(values, type=pa.bool_())
    if values.dtype.kind == "f":
        return pa.array(np.round(values, 2), type=pa.float64())
    return pa.array(values)


def write_csv(path: Path, cols: Columns) -> int:
    """Write a column dict as a UTF-8 CSV with a header row. Returns the row count.

    DuckDB's writer only quotes fields that need it (embedded commas/quotes), which keeps
    the landing files ~10% smaller than always-quoted output; padded whitespace survives.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({name: _to_arrow(values) for name, values in cols.items()})
    con = _duckdb()
    con.register("__raw_out", table)
    try:
        con.execute(f"COPY __raw_out TO {sql_path(path)} (HEADER, DELIMITER ',')")
    finally:
        con.unregister("__raw_out")
    return table.num_rows


def _json_value(v: object) -> object:
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 2)
    return v


def write_jsonl(path: Path, cols: Columns) -> int:
    """Write a column dict as newline-delimited JSON. Returns the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(cols)
    lists = [cols[name].tolist() for name in names]
    n = len(lists[0]) if lists else 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in zip(*lists, strict=True):
            record = {k: _json_value(v) for k, v in zip(names, row, strict=True)}
            fh.write(json.dumps(record, separators=(",", ":")))
            fh.write("\n")
    return n


def write_entity(
    raw_dir: Path, entity: str, batch: str, name: str, cols: Columns, fmt: str
) -> tuple[str, int]:
    """Write ``cols`` to ``raw_dir/entity/batch/name.<fmt>``; returns (relative path, rows)."""
    if not cols or not len(next(iter(cols.values()))):
        return "", 0
    path = raw_dir / entity / batch / f"{name}.{fmt}"
    rows = write_csv(path, cols) if fmt == "csv" else write_jsonl(path, cols)
    return path.relative_to(raw_dir).as_posix(), rows
