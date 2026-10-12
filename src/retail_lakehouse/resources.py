"""System-resource helpers: psutil-derived defaults, hardware summary and an RSS monitor."""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

GIB = 1024**3
MIB = 1024**2
ANONYMISED_HOST = "host-01"


def physical_cores() -> int:
    """Number of physical CPU cores (falls back to logical cores)."""
    return psutil.cpu_count(logical=False) or os.cpu_count() or 1


def logical_cores() -> int:
    """Number of logical CPU cores (hardware threads)."""
    return psutil.cpu_count(logical=True) or os.cpu_count() or 1


def default_workers() -> int:
    """Default generator worker processes: physical cores - 1 (at least 1)."""
    return max(1, physical_cores() - 1)


def default_duckdb_threads() -> int:
    """Default DuckDB threads: one per physical core."""
    return max(1, physical_cores())


def default_memory_limit_bytes() -> int:
    """DuckDB memory limit: min(2 GiB, 25% of total RAM, 50% of currently available RAM).

    The available-RAM term keeps the pipeline polite on a shared laptop; the result is
    floored at 256 MiB so tiny CI runners still work.
    """
    vm = psutil.virtual_memory()
    limit = min(2 * GIB, int(vm.total * 0.25), int(vm.available * 0.5))
    return max(limit, 256 * MIB)


def format_duckdb_memory(num_bytes: int) -> str:
    """Render bytes as a DuckDB memory_limit string such as ``'1536MB'``."""
    return f"{max(1, num_bytes // MIB)}MB"


def human_bytes(num_bytes: float) -> str:
    """Human readable byte count (ASCII only)."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def dir_size(path: Path, pattern: str = "*") -> int:
    """Total size in bytes of files under ``path`` matching ``pattern`` (recursive)."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob(pattern) if p.is_file())


def cpu_model() -> str:
    """Best-effort CPU model name without shelling out."""
    try:
        if sys.platform.startswith("win"):
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            )
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return " ".join(str(name).split())
        if sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return " ".join(line.split(":", 1)[1].split())
    except (OSError, ImportError):  # pragma: no cover - platform specific
        pass
    return platform.processor() or platform.machine() or "unknown"


def system_info() -> dict[str, Any]:
    """Anonymised hardware/software summary safe to publish (no hostname or user)."""
    vm = psutil.virtual_memory()
    import duckdb

    return {
        "host": ANONYMISED_HOST,
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "cpu_model": cpu_model(),
        "physical_cores": physical_cores(),
        "logical_cores": logical_cores(),
        "total_ram_gb": round(vm.total / GIB, 1),
        "available_ram_gb_at_start": round(vm.available / GIB, 1),
        "gpu_used": False,
    }


@dataclass
class ResourceSample:
    """Result of a :class:`ResourceMonitor` measurement."""

    wall_seconds: float = 0.0
    peak_rss_bytes: int = 0
    cpu_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def peak_rss_mb(self) -> float:
        return round(self.peak_rss_bytes / MIB, 1)


class ResourceMonitor:
    """Context manager measuring wall time, CPU time and peak RSS.

    Peak RSS is sampled in a background thread and covers this process *plus* its child
    processes (the multiprocessing generator workers), summed at each sample.
    """

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self.result = ResourceSample()
        self._proc = psutil.Process()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0
        self._cpu0 = 0.0

    def _rss_tree(self) -> int:
        total = 0
        try:
            total += self._proc.memory_info().rss
            for child in self._proc.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # pragma: no cover
            pass
        return total

    def _cpu_tree(self) -> float:
        times = self._proc.cpu_times()
        total = times.user + times.system
        total += getattr(times, "children_user", 0.0) + getattr(times, "children_system", 0.0)
        return total

    def _run(self) -> None:
        while not self._stop.is_set():
            self.result.peak_rss_bytes = max(self.result.peak_rss_bytes, self._rss_tree())
            self._stop.wait(self.interval)

    def __enter__(self) -> ResourceMonitor:
        self._t0 = time.perf_counter()
        self._cpu0 = self._cpu_tree()
        self.result.peak_rss_bytes = self._rss_tree()
        self._thread = threading.Thread(target=self._run, name="rss-monitor", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.result.peak_rss_bytes = max(self.result.peak_rss_bytes, self._rss_tree())
        self.result.wall_seconds = time.perf_counter() - self._t0
        self.result.cpu_seconds = max(0.0, self._cpu_tree() - self._cpu0)
