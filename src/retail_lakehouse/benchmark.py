"""Benchmark: generation scaling (1 vs N workers), per-layer wall time / peak RSS, DuckDB
thread scaling, Parquet compression ratio and storage footprint per layer.

Results are written to ``reports/benchmark.json`` (full detail) and ``docs/benchmark.json``
(a small, anonymised copy that the static report and the README table are built from).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from retail_lakehouse.bronze.contracts import CONTRACTS
from retail_lakehouse.bronze.ingest import ingest
from retail_lakehouse.config import Settings
from retail_lakehouse.generator.runner import GenerationSummary, generate
from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.quality.runner import run_quality
from retail_lakehouse.resources import MIB, ResourceMonitor, ResourceSample, dir_size, system_info
from retail_lakehouse.transform.runner import run_models
from retail_lakehouse.warehouse import compact

log = get_logger(__name__)
BENCHMARK_FILE = "benchmark.json"


def _measure(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, ResourceSample]:
    """Run ``fn`` under a :class:`ResourceMonitor`; return (result, sample)."""
    with ResourceMonitor() as mon:
        out = fn(*args, **kwargs)
    return out, mon.result


def _step(name: str, sample: ResourceSample, **extra: Any) -> dict[str, Any]:
    return {
        "step": name,
        "seconds": round(sample.wall_seconds, 3),
        "peak_rss_mb": sample.peak_rss_mb,
        "cpu_seconds": round(sample.cpu_seconds, 2),
        **extra,
    }


def compression_stats(settings: Settings) -> dict[str, Any]:
    """Raw (CSV / JSON Lines) bytes vs bronze Parquet (zstd) bytes, overall and per entity."""
    raw_dir, bronze_dir = settings.paths.raw_dir, settings.paths.bronze_dir
    per_entity: dict[str, dict[str, Any]] = {}
    for name, contract in CONTRACTS.items():
        raw = dir_size(raw_dir / name, f"*.{contract.extension}")
        parquet = dir_size(bronze_dir / name, "*.parquet")
        per_entity[name] = {
            "format": contract.extension,
            "raw_mb": round(raw / MIB, 2),
            "parquet_mb": round(parquet / MIB, 2),
            "ratio": round(raw / parquet, 2) if parquet else None,
        }
    csv_raw = sum(
        dir_size(raw_dir / n, "*.csv") for n, c in CONTRACTS.items() if c.extension == "csv"
    )
    csv_parquet = sum(
        dir_size(bronze_dir / n, "*.parquet") for n, c in CONTRACTS.items() if c.extension == "csv"
    )
    raw_total = sum(dir_size(raw_dir / n, f"*.{c.extension}") for n, c in CONTRACTS.items())
    parquet_total = dir_size(bronze_dir, "*.parquet")
    return {
        "overall_ratio": round(raw_total / parquet_total, 2) if parquet_total else None,
        "csv_only_ratio": round(csv_raw / csv_parquet, 2) if csv_parquet else None,
        "per_entity": per_entity,
    }


def storage_mb(settings: Settings) -> dict[str, float]:
    """Megabytes on disk per layer (raw landing zone, bronze Parquet, DuckDB warehouse)."""
    p = settings.paths
    return {
        "raw (CSV/JSONL)": round(dir_size(p.raw_dir) / MIB, 1),
        "bronze (Parquet)": round(dir_size(p.bronze_dir, "*.parquet") / MIB, 1),
        "silver+gold (DuckDB)": round(dir_size(p.warehouse_path) / MIB, 1),
    }


def _with_threads(settings: Settings, threads: int) -> Settings:
    res = dataclasses.replace(settings.resources, duckdb_threads=threads)
    return dataclasses.replace(settings, resources=res)


def run_benchmark(
    settings: Settings,
    workers_list: list[int] | None = None,
    update_readme: bool = False,
    thread_scaling: bool = True,
    repeats: int = 2,
) -> dict[str, Any]:
    """Run the benchmark and write ``benchmark.json``. Returns the result dict.

    The generator runs ``repeats`` times per entry of ``workers_list`` (default ``[1, auto]``)
    in a scratch directory under the OS temp dir and the fastest run is kept. A final
    generation into ``data/raw`` then feeds the remaining steps (ingest -> silver -> gold ->
    tests -> compaction), each measured for wall time, CPU time and peak RSS.
    """
    background_cpu = psutil.cpu_percent(interval=1.0)  # load from *other* processes
    auto = settings.resources.effective_workers()
    workers_list = workers_list or sorted({1, auto})
    log.info("benchmark: generation with workers=%s", workers_list)

    generation: list[dict[str, Any]] = []
    # Scaling runs write to a throw-away directory under the OS temp dir so the numbers
    # measure the generator, not a sync client / virus scanner watching the project folder.
    scratch = Path(tempfile.mkdtemp(prefix="rlh_bench_"))
    try:
        for w in workers_list:
            best: tuple[GenerationSummary, ResourceSample] | None = None
            for _ in range(max(1, repeats)):  # best-of-N damps noise from other workloads
                run = _measure(generate, settings, workers=w, raw_dir=scratch / "raw")
                if best is None or run[0].seconds < best[0].seconds:
                    best = run
            assert best is not None
            summary, sample = best
            generation.append(
                {
                    "workers": w,
                    "seconds": round(summary.seconds, 3),
                    "rows": summary.total_rows,
                    "rows_per_second": round(summary.rows_per_second),
                    "repeats": max(1, repeats),
                    "peak_rss_mb": sample.peak_rss_mb,
                }
            )
            log.info(
                "  workers=%d: %.2fs, %s rows/s, peak RSS %.0f MB",
                w,
                summary.seconds,
                f"{summary.rows_per_second:,.0f}",
                sample.peak_rss_mb,
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    base = generation[0]["seconds"]
    for g in generation:
        g["speedup"] = round(base / g["seconds"], 2) if g["seconds"] else None

    # The real landing zone feeds the rest of the pipeline.
    summary, sample = _measure(generate, settings, workers=workers_list[-1])
    steps: list[dict[str, Any]] = [
        _step("generate (into data/raw)", sample, workers=workers_list[-1])
    ]
    ing, sample = _measure(ingest, settings, full_refresh=True)
    steps.append(_step("ingest (bronze)", sample, rows=ing.rows, files=ing.new_files))
    _, sample = _measure(ingest, settings)
    steps.append(_step("ingest re-run (idempotent no-op)", sample))
    silver, sample = _measure(run_models, settings, layers=("silver",))
    steps.append(_step("transform silver", sample, models=len(silver.results)))
    gold, sample = _measure(run_models, settings, layers=("gold",))
    steps.append(_step("transform gold", sample, models=len(gold.results)))
    dq, sample = _measure(run_quality, settings, raise_on_error=False)
    steps.append(_step("data-quality tests", sample, tests=len(dq.results)))

    threads: list[dict[str, Any]] = []
    if thread_scaling:
        n = settings.resources.effective_threads()
        for t in sorted({1, n}):
            res, sample = _measure(
                run_models, _with_threads(settings, t), layers=("silver", "gold")
            )
            threads.append(
                {"threads": t, "seconds": round(res.seconds, 3), "peak_rss_mb": sample.peak_rss_mb}
            )
        for row in threads:
            row["speedup"] = round(threads[0]["seconds"] / row["seconds"], 2)

    _, sample = _measure(compact, settings)
    steps.append(_step("compact warehouse", sample))

    result: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "system": system_info(),
        "background_cpu_percent": background_cpu,
        "settings": {
            "customers": settings.generator.customers,
            "start": settings.generator.start.isoformat(),
            "end": settings.generator.resolved_end().isoformat(),
            "seed": settings.generator.seed,
            "duckdb_threads": settings.resources.effective_threads(),
            "duckdb_memory_limit": settings.resources.effective_memory_limit(),
        },
        "generation": generation,
        "steps": steps,
        "transform_thread_scaling": threads,
        "compression": compression_stats(settings),
        "storage_mb": storage_mb(settings),
        "rows_by_entity": summary.rows if summary else {},
    }
    out_paths = [
        settings.paths.reports_dir / BENCHMARK_FILE,
        settings.paths.docs_dir / BENCHMARK_FILE,
    ]
    for path in out_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log.info("benchmark written to %s", " and ".join(p.as_posix() for p in out_paths))
    if update_readme:
        from retail_lakehouse.reporting.build import update_readme_block

        ok = update_readme_block(
            Path(settings.paths.readme), "BENCHMARK", benchmark_markdown(result)
        )
        log.info("README benchmark block %s", "updated" if ok else "not found (markers missing)")
    return result


def benchmark_markdown(result: dict[str, Any]) -> str:
    """Markdown tables summarising a benchmark result (for the README)."""
    sysinfo = result["system"]
    lines = [
        f"Measured on {sysinfo['cpu_model']} ({sysinfo['physical_cores']} cores / "
        f"{sysinfo['logical_cores']} threads, {sysinfo['total_ram_gb']} GB RAM), "
        f"{sysinfo['os']}, Python {sysinfo['python']}, DuckDB {sysinfo['duckdb']}; "
        f"{result['settings']['customers']:,} customers, "
        f"{result['settings']['start']} to {result['settings']['end']}.",
        "",
    ]
    load = result.get("background_cpu_percent")
    if load is not None and load >= 25:
        lines += [
            f"> Measured on a shared laptop while other workloads kept the CPU ~{load:.0f}% busy, "
            "so multi-process and multi-thread speed-ups are understated.",
            "",
        ]
    lines += [
        "| Generation | Wall time | Throughput | Speed-up | Peak RSS (all processes) |",
        "|---|---:|---:|---:|---:|",
    ]
    for g in result["generation"]:
        lines.append(
            f"| {g['workers']} worker{'s' if g['workers'] > 1 else ''} | {g['seconds']:.1f} s | "
            f"{g['rows_per_second']:,.0f} rows/s | {g['speedup']:.2f}x | {g['peak_rss_mb']:,.0f} MB |"
        )
    lines += [
        "",
        "| Pipeline step | Wall time | Peak RSS | CPU time* |",
        "|---|---:|---:|---:|",
    ]
    for s in result["steps"]:
        lines.append(
            f"| {s['step']} | {s['seconds']:.2f} s | {s['peak_rss_mb']:,.0f} MB | "
            f"{s['cpu_seconds']:.1f} s |"
        )
    lines += [
        "",
        r"\* user + system CPU of the main process (plus reaped child processes on Linux/macOS);"
        " peak RSS is sampled every 50 ms and summed over the process tree.",
    ]
    if result.get("transform_thread_scaling"):
        lines += [
            "",
            "| DuckDB threads (silver + gold) | Wall time | Speed-up |",
            "|---|---:|---:|",
        ]
        for t in result["transform_thread_scaling"]:
            lines.append(f"| {t['threads']} | {t['seconds']:.2f} s | {t['speedup']:.2f}x |")
    comp = result["compression"]
    lines += ["", "| Storage layer | Size on disk |", "|---|---:|"]
    for name, mb in result["storage_mb"].items():
        lines.append(f"| {name} | {mb:,.1f} MB |")
    lines += [
        "",
        f"Bronze Parquet (zstd) is **{comp['overall_ratio']}x** smaller than the raw landing "
        f"zone overall and **{comp['csv_only_ratio']}x** smaller for the CSV entities.",
    ]
    return "\n".join(lines)
