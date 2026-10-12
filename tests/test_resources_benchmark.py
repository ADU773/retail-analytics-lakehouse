from __future__ import annotations

import json
import time
from pathlib import Path

from retail_lakehouse import resources
from retail_lakehouse.benchmark import benchmark_markdown, run_benchmark
from retail_lakehouse.pipeline import clean


def test_formatting_helpers() -> None:
    assert resources.format_duckdb_memory(1536 * resources.MIB) == "1536MB"
    assert resources.human_bytes(512) == "512 B"
    assert resources.human_bytes(3 * resources.MIB) == "3.0 MB"


def test_dir_size(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.parquet").write_bytes(b"0" * 100)
    (tmp_path / "a" / "y.csv").write_bytes(b"0" * 50)
    assert resources.dir_size(tmp_path) == 150
    assert resources.dir_size(tmp_path, "*.parquet") == 100
    assert resources.dir_size(tmp_path / "missing") == 0


def test_resource_monitor_measures_wall_and_rss() -> None:
    with resources.ResourceMonitor(interval=0.01) as mon:
        blob = bytearray(20 * resources.MIB)
        time.sleep(0.05)
    del blob
    assert mon.result.wall_seconds >= 0.05
    assert mon.result.peak_rss_mb > 20


def test_system_info_is_anonymised() -> None:
    import platform

    info = resources.system_info()
    assert info["host"] == "host-01"
    assert platform.node() not in json.dumps(info)


def test_benchmark_and_clean(small_settings) -> None:
    result = run_benchmark(small_settings, workers_list=[1, 2], thread_scaling=False, repeats=1)
    assert [g["workers"] for g in result["generation"]] == [1, 2]
    assert all(g["rows_per_second"] > 0 for g in result["generation"])
    assert result["compression"]["overall_ratio"] > 1  # Parquet beats CSV / JSON Lines
    steps = {s["step"] for s in result["steps"]}
    assert {"ingest (bronze)", "transform silver", "transform gold"} <= steps
    for path in (small_settings.paths.reports_dir, small_settings.paths.docs_dir):
        assert (path / "benchmark.json").exists()
    md = benchmark_markdown(result)
    assert "rows/s" in md and "| Pipeline step |" in md

    removed = clean(small_settings)
    assert removed and not small_settings.paths.bronze_dir.exists()
    assert (small_settings.paths.docs_dir / "benchmark.json").exists()  # docs/ is never cleaned
