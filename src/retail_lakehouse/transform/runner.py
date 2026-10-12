"""Execute the model DAG against the DuckDB warehouse and record a run log."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import jinja2
import pandas as pd

from retail_lakehouse.bronze.contracts import CONTRACTS
from retail_lakehouse.config import Settings
from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.transform.dag import Dag
from retail_lakehouse.transform.models import Model, load_sql_models
from retail_lakehouse.transform.python_models import PYTHON_MODELS, ModelContext
from retail_lakehouse.warehouse import connect, register_bronze_views, sql_root

log = get_logger(__name__)

RUN_LOG_DDL = """
CREATE TABLE IF NOT EXISTS meta.run_log (
    run_id VARCHAR, model VARCHAR, layer VARCHAR, kind VARCHAR, materialized VARCHAR,
    status VARCHAR, rows BIGINT, quarantined_rows BIGINT, started_at TIMESTAMP,
    seconds DOUBLE, error VARCHAR
)
"""


class TransformError(RuntimeError):
    """One or more models failed."""


@dataclass
class ModelResult:
    """Outcome of one model execution."""

    name: str
    layer: str
    kind: str
    materialized: str
    status: str  # success | error | skipped
    rows: int = 0
    quarantined_rows: int = 0
    seconds: float = 0.0
    error: str | None = None


@dataclass
class RunResult:
    """Outcome of a DAG run."""

    run_id: str
    results: list[ModelResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def failed(self) -> list[ModelResult]:
        return [r for r in self.results if r.status != "success"]

    def by_layer(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.results:
            out[r.layer] = out.get(r.layer, 0.0) + r.seconds
        return out


def bronze_sources() -> set[str]:
    """Names of the bronze sources the DAG may depend on."""
    return {f"bronze.{name}" for name in CONTRACTS}


def discover_models(settings: Settings) -> dict[str, Model]:
    """All SQL models from the SQL directory plus registered Python models."""
    models = load_sql_models(sql_root(settings))
    for name, model in PYTHON_MODELS.items():
        if name in models:
            raise TransformError(f"python model {name} clashes with a SQL model")
        models[name] = model
    return models


def build_dag(settings: Settings) -> Dag:
    """Validated DAG of every model."""
    return Dag(discover_models(settings), sources=bronze_sources())


def render_sql(sql: str, settings: Settings) -> str:
    """Render ``{{ var }}`` placeholders (Jinja) in model SQL."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    return env.from_string(sql).render(
        as_of=settings.as_of.isoformat(),
        start_date=settings.generator.start.isoformat(),
    )


def _materialize(
    con: duckdb.DuckDBPyConnection, model: Model, settings: Settings, ctx: ModelContext
) -> tuple[int, int]:
    """Build one model; returns (rows, quarantined_rows)."""
    if model.kind == "python":
        assert model.func is not None
        df = model.func(con, ctx)
        if not isinstance(df, pd.DataFrame):
            raise TransformError(f"python model {model.name} must return a pandas DataFrame")
        con.register("__py_model_df", df)
        try:
            con.execute(f"CREATE OR REPLACE TABLE {model.name} AS SELECT * FROM __py_model_df")
        finally:
            con.unregister("__py_model_df")
        return len(df), 0

    body = render_sql(model.sql or "", settings)
    if model.materialized == "view":
        con.execute(f"CREATE OR REPLACE VIEW {model.name} AS\n{body}")
        return int(con.execute(f"SELECT count(*) FROM {model.name}").fetchone()[0]), 0
    if model.quarantine:
        con.execute(f"CREATE OR REPLACE TEMP TABLE __stage AS\n{body}")
        cols = [r[0] for r in con.execute("DESCRIBE __stage").fetchall()]
        if "rejection_reason" not in cols:
            raise TransformError(f"{model.name}: quarantine models must emit rejection_reason")
        raw_cols = [c for c in cols if c.startswith("raw_")]
        exclude = ", ".join(["rejection_reason", *raw_cols])
        con.execute(
            f"CREATE OR REPLACE TABLE {model.name} AS "
            f"SELECT * EXCLUDE ({exclude}) FROM __stage WHERE rejection_reason IS NULL"
        )
        con.execute(
            f"CREATE OR REPLACE TABLE {model.quarantine} AS "
            "SELECT rejection_reason, * EXCLUDE (rejection_reason), "
            "current_timestamp::TIMESTAMP AS _quarantined_at "
            "FROM __stage WHERE rejection_reason IS NOT NULL"
        )
        con.execute("DROP TABLE __stage")
        rows = con.execute(f"SELECT count(*) FROM {model.name}").fetchone()[0]
        bad = con.execute(f"SELECT count(*) FROM {model.quarantine}").fetchone()[0]
        return int(rows), int(bad)
    con.execute(f"CREATE OR REPLACE TABLE {model.name} AS\n{body}")
    return int(con.execute(f"SELECT count(*) FROM {model.name}").fetchone()[0]), 0


def run_models(
    settings: Settings,
    select: list[str] | None = None,
    layers: tuple[str, ...] | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    fail_fast: bool = False,
) -> RunResult:
    """Run (a selection of) the DAG in dependency order.

    Failed models are logged; their downstream models are skipped. A :class:`TransformError`
    is raised at the end when anything failed, so the CLI exits non-zero.
    """
    t0 = time.perf_counter()
    dag = build_dag(settings)
    order = dag.select(select) if select else dag.topological_order()
    if layers:
        order = [n for n in order if dag.models[n].layer in layers]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    result = RunResult(run_id=run_id)
    own_con = con is None
    con = con or connect(settings)
    try:
        register_bronze_views(con, Path(settings.paths.bronze_dir))
        con.execute(RUN_LOG_DDL)
        ctx = ModelContext(as_of=settings.as_of, run_id=run_id)
        broken: set[str] = set()
        log.info("running %d model(s) [run %s]", len(order), run_id)
        for name in order:
            model = dag.models[name]
            started = datetime.now(UTC).replace(tzinfo=None)
            tm = time.perf_counter()
            res = ModelResult(name, model.layer, model.kind, model.materialized, "success")
            upstream_broken = dag.ancestors(name) & broken
            if upstream_broken:
                res.status = "skipped"
                res.error = f"upstream failed: {', '.join(sorted(upstream_broken))}"
                broken.add(name)
            else:
                try:
                    res.rows, res.quarantined_rows = _materialize(con, model, settings, ctx)
                except (duckdb.Error, TransformError, ValueError, RuntimeError) as exc:
                    res.status = "error"
                    res.error = str(exc).splitlines()[0][:500]
                    broken.add(name)
            res.seconds = time.perf_counter() - tm
            result.results.append(res)
            con.execute(
                "INSERT INTO meta.run_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run_id,
                    name,
                    model.layer,
                    model.kind,
                    model.materialized,
                    res.status,
                    res.rows,
                    res.quarantined_rows,
                    started,
                    res.seconds,
                    res.error,
                ],
            )
            level = "info" if res.status == "success" else "error"
            q = f" quarantined={res.quarantined_rows:,}" if res.quarantined_rows else ""
            getattr(log, level)(
                "%-7s %-28s rows=%-9s%s (%.2fs)%s",
                res.status.upper(),
                name,
                f"{res.rows:,}",
                q,
                res.seconds,
                f" - {res.error}" if res.error else "",
            )
            if fail_fast and res.status == "error":
                break
    finally:
        if own_con:
            con.close()
    result.seconds = time.perf_counter() - t0
    if result.failed:
        names = ", ".join(f"{r.name} ({r.status})" for r in result.failed)
        raise TransformError(f"{len(result.failed)} model(s) did not succeed: {names}")
    log.info("transform finished: %d model(s) in %.1fs", len(result.results), result.seconds)
    return result
