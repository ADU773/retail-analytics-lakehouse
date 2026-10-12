"""Execute data-quality tests, persist results and write JSON + Markdown reports."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path

import duckdb

from retail_lakehouse.config import Settings
from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.quality.checks import compile_test, evaluate
from retail_lakehouse.quality.spec import LayerSpec, TestCase, load_spec
from retail_lakehouse.warehouse import connect, register_bronze_views

log = get_logger(__name__)
LAYERS = ("bronze", "silver", "gold")

RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS meta.dq_results (
    run_id VARCHAR, executed_at TIMESTAMP, layer VARCHAR, test_id VARCHAR, table_name VARCHAR,
    column_name VARCHAR, test VARCHAR, severity VARCHAR, status VARCHAR, failures BIGINT,
    total BIGINT, failure_rate DOUBLE, seconds DOUBLE, message VARCHAR
)
"""


class DataQualityError(RuntimeError):
    """Raised when an error-severity test fails."""


@dataclass
class TestResult:
    """Outcome of one test."""

    layer: str
    test_id: str
    table: str
    column: str | None
    test: str
    severity: str
    status: str  # pass | warn | fail | error
    failures: int
    total: int
    failure_rate: float
    seconds: float
    message: str = ""
    query: str = ""


@dataclass
class DQReport:
    """Results of a data-quality run."""

    run_id: str
    as_of: date
    results: list[TestResult] = field(default_factory=list)
    seconds: float = 0.0

    def counts(self, layer: str | None = None) -> dict[str, int]:
        out = {"pass": 0, "warn": 0, "fail": 0, "error": 0}
        for r in self.results:
            if layer is None or r.layer == layer:
                out[r.status] += 1
        return out

    @property
    def failed(self) -> list[TestResult]:
        return [r for r in self.results if r.status in ("fail", "error")]

    @property
    def warned(self) -> list[TestResult]:
        return [r for r in self.results if r.status == "warn"]


def dq_dir(settings: Settings) -> Path:
    """Directory holding ``<layer>.yaml`` test specifications."""
    if settings.paths.dq_dir is not None:
        return Path(settings.paths.dq_dir)
    return Path(str(resources.files("retail_lakehouse").joinpath("dq")))


def load_layer(settings: Settings, layer: str) -> LayerSpec:
    """Load the YAML spec for one layer."""
    path = dq_dir(settings) / f"{layer}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no data-quality spec for layer {layer!r} at {path}")
    return load_spec(path)


def _run_case(con: duckdb.DuckDBPyConnection, case: TestCase, as_of: date) -> TestResult:
    sql = compile_test(case, as_of)
    t0 = time.perf_counter()
    try:
        failures, total = con.execute(sql).fetchone()  # type: ignore[misc]
        failures, total = int(failures or 0), int(total or 0)
        status = evaluate(case, failures, total)
        message = ""
        if case.test == "freshness":
            message = f"newest row is {total} day(s) old (max {case.params['max_age_days']})"
        elif case.test == "row_count_min":
            message = f"{total:,} rows (min {case.params['min']:,})"
        elif failures:
            message = f"{failures:,} of {total:,} rows failed"
    except duckdb.Error as exc:
        failures, total, status = 0, 0, "error"
        message = str(exc).splitlines()[0][:300]
    rate = failures / total if total and case.test not in ("row_count_min", "freshness") else 0.0
    return TestResult(
        layer=case.layer,
        test_id=case.test_id,
        table=case.table,
        column=case.column,
        test=case.test,
        severity=case.severity,
        status=status,
        failures=failures,
        total=total,
        failure_rate=round(rate, 6),
        seconds=round(time.perf_counter() - t0, 4),
        message=message,
        query=sql,
    )


def run_quality(
    settings: Settings,
    layers: tuple[str, ...] = LAYERS,
    con: duckdb.DuckDBPyConnection | None = None,
    raise_on_error: bool | None = None,
) -> DQReport:
    """Run all tests for ``layers``; persist and report them.

    Raises :class:`DataQualityError` when an error-severity test fails (unless disabled via
    ``quality.fail_on_error: false`` or ``raise_on_error=False``).
    """
    t0 = time.perf_counter()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    report = DQReport(run_id=run_id, as_of=settings.as_of)
    own = con is None
    con = con or connect(settings)
    try:
        register_bronze_views(con, Path(settings.paths.bronze_dir))
        con.execute(RESULTS_DDL)
        executed_at = datetime.now(UTC).replace(tzinfo=None)
        for layer in layers:
            spec = load_layer(settings, layer)
            layer_results = [_run_case(con, case, settings.as_of) for case in spec.tests]
            report.results += layer_results
            c = report.counts(layer)
            log.info(
                "dq %-6s %3d tests: %d pass, %d warn, %d fail, %d error",
                layer,
                len(layer_results),
                c["pass"],
                c["warn"],
                c["fail"],
                c["error"],
            )
            for r in layer_results:
                if r.status != "pass":
                    lvl = log.warning if r.status == "warn" else log.error
                    lvl("  %-5s %s - %s", r.status.upper(), r.test_id, r.message)
        con.executemany(
            "INSERT INTO meta.dq_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    run_id,
                    executed_at,
                    r.layer,
                    r.test_id,
                    r.table,
                    r.column,
                    r.test,
                    r.severity,
                    r.status,
                    r.failures,
                    r.total,
                    r.failure_rate,
                    r.seconds,
                    r.message,
                ]
                for r in report.results
            ],
        )
    finally:
        if own:
            con.close()
    report.seconds = time.perf_counter() - t0
    write_reports(report, settings.paths.dq_reports_dir, layers)

    should_raise = settings.quality.fail_on_error if raise_on_error is None else raise_on_error
    if report.failed and should_raise:
        names = "; ".join(r.test_id for r in report.failed[:10])
        raise DataQualityError(
            f"{len(report.failed)} error-severity data-quality test(s) failed: {names}"
        )
    return report


def report_to_dict(report: DQReport) -> dict[str, object]:
    """JSON-serialisable report."""
    return {
        "run_id": report.run_id,
        "as_of": report.as_of.isoformat(),
        "seconds": round(report.seconds, 3),
        "summary": report.counts(),
        "by_layer": {
            layer: report.counts(layer) for layer in sorted({r.layer for r in report.results})
        },
        "results": [{k: v for k, v in asdict(r).items() if k != "query"} for r in report.results],
    }


def report_to_markdown(report: DQReport) -> str:
    """Human-readable Markdown report."""
    c = report.counts()
    lines = [
        "# Data-quality report",
        "",
        f"Run `{report.run_id}` - as-of date {report.as_of.isoformat()} - "
        f"{len(report.results)} tests in {report.seconds:.1f}s.",
        "",
        f"**{c['pass']} passed, {c['warn']} warnings, {c['fail']} failed, {c['error']} errors.**",
        "",
        "| Layer | Tests | Pass | Warn | Fail | Error |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for layer in sorted({r.layer for r in report.results}, key=LAYERS.index):
        lc = report.counts(layer)
        total = sum(lc.values())
        lines.append(
            f"| {layer} | {total} | {lc['pass']} | {lc['warn']} | {lc['fail']} | {lc['error']} |"
        )
    issues = [r for r in report.results if r.status != "pass"]
    lines += ["", "## Findings (non-passing tests)", ""]
    if not issues:
        lines.append("All tests passed.")
    else:
        lines += ["| Status | Severity | Test | Detail |", "|---|---|---|---|"]
        for r in issues:
            detail = r.message.replace("|", "\\|")
            lines.append(f"| {r.status.upper()} | {r.severity} | `{r.test_id}` | {detail} |")
    lines += [
        "",
        "## All tests",
        "",
        "| Layer | Test | Status | Failing rows | Rows checked |",
        "|---|---|---|---:|---:|",
    ]
    for r in report.results:
        lines.append(f"| {r.layer} | `{r.test_id}` | {r.status} | {r.failures:,} | {r.total:,} |")
    return "\n".join(lines) + "\n"


def write_reports(report: DQReport, out_dir: Path, layers: tuple[str, ...]) -> list[Path]:
    """Write ``dq_<layers>.json`` / ``.md`` plus ``latest.json`` / ``latest.md``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "_".join(layers)
    data = json.dumps(report_to_dict(report), indent=2)
    md = report_to_markdown(report)
    paths = []
    for stem in (f"dq_{tag}", "latest"):
        (out_dir / f"{stem}.json").write_text(data, encoding="utf-8")
        (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")
        paths += [out_dir / f"{stem}.json", out_dir / f"{stem}.md"]
    return paths
