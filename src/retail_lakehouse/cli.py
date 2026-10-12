"""Command-line interface: ``retail-lakehouse <command>`` (also ``python -m retail_lakehouse``)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from typing import Any

from retail_lakehouse import __version__
from retail_lakehouse.config import ConfigError, Settings, load_settings
from retail_lakehouse.logging_setup import configure_logging, get_logger

log = get_logger("cli")


def _add_generator_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("data generation")
    g.add_argument("--scale", choices=["tiny", "small", "default"], help="size preset")
    g.add_argument("--customers", type=int, help="number of customers")
    g.add_argument("--days", type=int, help="length of the period in days (from --start)")
    g.add_argument("--start", type=date.fromisoformat, help="first day (YYYY-MM-DD)")
    g.add_argument("--end", type=date.fromisoformat, help="last day (YYYY-MM-DD)")
    g.add_argument("--seed", type=int, help="random seed (output is reproducible per seed)")
    g.add_argument("--workers", type=int, help="generator processes (default: physical cores - 1)")


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for all subcommands."""
    parser = argparse.ArgumentParser(
        prog="retail-lakehouse",
        description="Aurora Goods Co. analytics lakehouse: DuckDB + Parquet, bronze/silver/gold.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="YAML config file (default: ./lakehouse.yaml if present)")
    parser.add_argument("--data-dir", help="root for raw/, bronze/ and warehouse/")
    parser.add_argument("--reports-dir", help="where DQ reports and benchmark.json go")
    parser.add_argument("--docs-dir", help="where the static report is written")
    parser.add_argument("--threads", type=int, help="DuckDB threads (default: physical cores)")
    parser.add_argument("--memory-limit", help="DuckDB memory_limit, e.g. 1GB (default: auto)")
    parser.add_argument("--as-of", type=date.fromisoformat, help="reference date for freshness/RFM")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("generate", help="generate synthetic raw data (CSV / JSON Lines)")
    _add_generator_args(p)

    p = sub.add_parser("ingest", help="ingest raw files into bronze Parquet (idempotent)")
    p.add_argument("--full-refresh", action="store_true", help="rebuild bronze from scratch")

    p = sub.add_parser("transform", help="run silver/gold models through the DAG runner")
    p.add_argument("--select", nargs="+", help="models to run, e.g. +gold.mart_rfm silver.orders+")
    p.add_argument("--layer", choices=["silver", "gold"], action="append", help="restrict to layer")

    p = sub.add_parser("test", help="run declarative data-quality tests")
    p.add_argument("--layer", choices=["bronze", "silver", "gold"], action="append")
    p.add_argument("--no-fail", action="store_true", help="always exit 0 (report only)")

    p = sub.add_parser("report", help="build docs/index.html, docs/images/*.png and insights")
    p.add_argument("--update-readme", action="store_true", help="refresh README key-results block")

    p = sub.add_parser("run-all", help="generate -> ingest -> transform -> test -> report")
    _add_generator_args(p)
    p.add_argument("--skip-report", action="store_true")
    p.add_argument("--update-readme", action="store_true")

    p = sub.add_parser("benchmark", help="measure throughput, scaling, RSS and storage")
    _add_generator_args(p)
    p.add_argument("--workers-list", help="comma-separated worker counts (default: 1,<auto>)")
    p.add_argument(
        "--repeats", type=int, default=2, help="generation runs per worker count (best kept)"
    )
    p.add_argument("--update-readme", action="store_true", help="refresh README benchmark table")

    p = sub.add_parser("lineage", help="print the model DAG (text or Mermaid)")
    p.add_argument("--format", choices=["text", "mermaid"], default="text")
    p.add_argument("--select", nargs="+")

    p = sub.add_parser("clean", help="delete generated data and reports (never docs/)")
    p.add_argument("--keep-raw", action="store_true", help="keep the raw landing zone")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Merge CLI flags into the layered configuration."""
    overrides: dict[str, Any] = {"paths": {}, "generator": {}, "resources": {}, "quality": {}}
    for flag, (section, key) in {
        "data_dir": ("paths", "data_dir"),
        "reports_dir": ("paths", "reports_dir"),
        "docs_dir": ("paths", "docs_dir"),
        "threads": ("resources", "duckdb_threads"),
        "memory_limit": ("resources", "duckdb_memory_limit"),
        "workers": ("resources", "workers"),
        "customers": ("generator", "customers"),
        "days": ("generator", "days"),
        "start": ("generator", "start"),
        "end": ("generator", "end"),
        "seed": ("generator", "seed"),
        "as_of": ("quality", "as_of"),
    }.items():
        value = getattr(args, flag, None)
        if value is not None:
            overrides[section][key] = value
    if getattr(args, "end", None) is not None and getattr(args, "days", None) is None:
        overrides["generator"]["days"] = None
    return load_settings(args.config, overrides=overrides, scale=getattr(args, "scale", None))


def _cmd_lineage(settings: Settings, fmt: str, select: list[str] | None) -> None:
    from retail_lakehouse.transform.runner import build_dag

    dag = build_dag(settings)
    order = dag.select(select) if select else dag.topological_order()
    if fmt == "text":
        for name in order:
            model = dag.models[name]
            deps = ", ".join(model.depends_on) or "-"
            print(f"{name:32s} [{model.kind}/{model.materialized}] <- {deps}")
        return
    print("flowchart LR")
    nodes = set(order)
    for name in order:
        for dep in sorted(dag.models[name].depends_on):
            src = dag.produced_by.get(dep, dep)
            if src in nodes or src.startswith("bronze."):
                print(
                    f'    {src.replace(".", "_")}["{src}"] --> {name.replace(".", "_")}["{name}"]'
                )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    try:
        settings = settings_from_args(args)
        cmd = args.command
        if cmd == "generate":
            from retail_lakehouse.generator.runner import generate

            generate(settings, workers=args.workers)
        elif cmd == "ingest":
            from retail_lakehouse.bronze.ingest import ingest

            ingest(settings, full_refresh=args.full_refresh)
        elif cmd == "transform":
            from retail_lakehouse.transform.runner import run_models

            run_models(
                settings, select=args.select, layers=tuple(args.layer) if args.layer else None
            )
        elif cmd == "test":
            from retail_lakehouse.quality.runner import LAYERS, run_quality

            layers = tuple(args.layer) if args.layer else LAYERS
            run_quality(settings, layers=layers, raise_on_error=False if args.no_fail else None)
        elif cmd == "report":
            from retail_lakehouse.reporting.build import build_report

            build_report(settings, update_readme=args.update_readme)
        elif cmd == "run-all":
            from retail_lakehouse.pipeline import run_all

            run_all(
                settings,
                workers=args.workers,
                skip_report=args.skip_report,
                update_readme=args.update_readme,
            )
        elif cmd == "benchmark":
            from retail_lakehouse.benchmark import run_benchmark

            workers_list = (
                [int(w) for w in args.workers_list.split(",")] if args.workers_list else None
            )
            run_benchmark(
                settings,
                workers_list=workers_list,
                update_readme=args.update_readme,
                repeats=args.repeats,
            )
        elif cmd == "lineage":
            _cmd_lineage(settings, args.format, args.select)
        elif cmd == "clean":
            from retail_lakehouse.pipeline import clean

            clean(settings, keep_raw=args.keep_raw)
        return 0
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        log.error("interrupted")
        return 130
    except Exception as exc:
        from retail_lakehouse.bronze.ingest import IngestError
        from retail_lakehouse.quality.runner import DataQualityError
        from retail_lakehouse.transform.dag import DagError
        from retail_lakehouse.transform.runner import TransformError

        known = (IngestError, DataQualityError, DagError, TransformError, FileNotFoundError)
        if isinstance(exc, known):
            log.error("%s: %s", type(exc).__name__, exc)
            return 1
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
