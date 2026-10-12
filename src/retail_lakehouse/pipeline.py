"""High-level pipeline steps shared by the CLI, the benchmark and the tests."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from retail_lakehouse.bronze.ingest import IngestSummary, ingest
from retail_lakehouse.config import Settings
from retail_lakehouse.generator.runner import GenerationSummary, generate
from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.quality.runner import DQReport, run_quality
from retail_lakehouse.resources import ResourceMonitor, dir_size, system_info
from retail_lakehouse.transform.runner import RunResult, run_models
from retail_lakehouse.warehouse import compact

log = get_logger(__name__)
RUN_SUMMARY = "run_summary.json"


@dataclass
class StepTiming:
    """Wall time / peak RSS of one pipeline step."""

    step: str
    seconds: float
    peak_rss_mb: float
    cpu_seconds: float


@dataclass
class PipelineRun:
    """Everything a ``run-all`` produced."""

    steps: list[StepTiming] = field(default_factory=list)
    generation: GenerationSummary | None = None
    ingest: IngestSummary | None = None
    transforms: list[RunResult] = field(default_factory=list)
    dq: list[DQReport] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.steps)


def storage_footprint(settings: Settings) -> dict[str, int]:
    """Bytes on disk per layer."""
    p = settings.paths
    return {
        "raw": dir_size(p.raw_dir),
        "bronze": dir_size(p.bronze_dir, "*.parquet"),
        "warehouse": dir_size(p.warehouse_path),
    }


def _timed(run: PipelineRun, name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    with ResourceMonitor() as mon:
        out = fn(*args, **kwargs)
    r = mon.result
    run.steps.append(
        StepTiming(name, round(r.wall_seconds, 3), r.peak_rss_mb, round(r.cpu_seconds, 2))
    )
    log.info("step %-16s %.2fs (peak RSS %.0f MB)", name, r.wall_seconds, r.peak_rss_mb)
    return out


def run_all(
    settings: Settings,
    workers: int | None = None,
    skip_report: bool = False,
    update_readme: bool = False,
) -> PipelineRun:
    """generate -> ingest -> test(bronze) -> silver -> test(silver) -> gold -> test(gold)
    -> compact -> report."""
    run = PipelineRun()
    run.generation = _timed(run, "generate", generate, settings, workers=workers)
    run.ingest = _timed(run, "ingest", ingest, settings, refresh_on_new_dataset=True)
    run.dq.append(_timed(run, "test_bronze", run_quality, settings, layers=("bronze",)))
    run.transforms.append(_timed(run, "transform_silver", run_models, settings, layers=("silver",)))
    run.dq.append(_timed(run, "test_silver", run_quality, settings, layers=("silver",)))
    run.transforms.append(_timed(run, "transform_gold", run_models, settings, layers=("gold",)))
    run.dq.append(_timed(run, "test_gold", run_quality, settings, layers=("gold",)))
    _timed(run, "compact", compact, settings)
    write_run_summary(settings, run)
    if not skip_report:
        from retail_lakehouse.reporting.build import build_report

        _timed(run, "report", build_report, settings, update_readme=update_readme)
        write_run_summary(settings, run)
    log.info("run-all finished in %.1fs", run.total_seconds)
    return run


def write_run_summary(settings: Settings, run: PipelineRun) -> Path:
    """Persist step timings, row counts, DQ outcome and storage footprint as JSON."""
    gen = run.generation
    dq_counts: dict[str, dict[str, int]] = {}
    for rep in run.dq:
        for layer in {r.layer for r in rep.results}:
            dq_counts[layer] = rep.counts(layer)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "system": system_info(),
        "settings": {
            "customers": settings.generator.customers,
            "start": settings.generator.start.isoformat(),
            "end": settings.generator.resolved_end().isoformat(),
            "seed": settings.generator.seed,
            "workers": gen.workers if gen else settings.resources.effective_workers(),
            "duckdb_threads": settings.resources.effective_threads(),
            "duckdb_memory_limit": settings.resources.effective_memory_limit(),
        },
        "steps": [s.__dict__ for s in run.steps],
        "total_seconds": round(run.total_seconds, 2),
        "peak_rss_mb": max((s.peak_rss_mb for s in run.steps), default=0.0),
        "generation": {
            "rows": gen.rows,
            "total_rows": gen.total_rows,
            "files": gen.files,
            "batches": len(gen.batch_ids),
            "rows_per_second": round(gen.rows_per_second),
        }
        if gen
        else None,
        "models": [
            {
                "model": r.name,
                "status": r.status,
                "rows": r.rows,
                "quarantined_rows": r.quarantined_rows,
                "seconds": round(r.seconds, 3),
            }
            for t in run.transforms
            for r in t.results
        ],
        "dq": dq_counts,
        "storage_bytes": storage_footprint(settings),
    }
    out = settings.paths.reports_dir / RUN_SUMMARY
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def clean(settings: Settings, keep_raw: bool = False, include_reports: bool = True) -> list[Path]:
    """Delete generated data (and reports). Never touches ``docs/``."""
    p = settings.paths
    targets = [p.bronze_dir, p.warehouse_dir]
    if not keep_raw:
        targets.append(p.raw_dir)
    if include_reports:
        targets.append(p.reports_dir)
    removed = []
    for target in targets:
        target = Path(target)
        if target.exists():
            resolved = target.resolve()
            if len(resolved.parts) <= 2:  # refuse to delete drive roots / top-level folders
                raise RuntimeError(f"refusing to delete suspicious path {resolved}")
            shutil.rmtree(target)
            removed.append(target)
            log.info("removed %s", target)
    data_dir = Path(p.data_dir)
    if data_dir.exists() and not any(data_dir.iterdir()):
        data_dir.rmdir()
    return removed
