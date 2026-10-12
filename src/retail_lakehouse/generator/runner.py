"""Generation orchestrator: global skeleton in the parent, monthly chunks in a process pool."""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import pickle
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from retail_lakehouse.config import Settings
from retail_lakehouse.generator import catalog
from retail_lakehouse.generator import quality as dq
from retail_lakehouse.generator.behavior import simulate_orders
from retail_lakehouse.generator.calendar import build_daily_profile
from retail_lakehouse.generator.catalog import build_products
from retail_lakehouse.generator.chunk import (
    ChunkResult,
    ChunkTask,
    GeneratorParams,
    SharedState,
    batch_for,
    init_worker,
    run_chunk,
)
from retail_lakehouse.generator.customers import build_customers
from retail_lakehouse.generator.writer import write_entity
from retail_lakehouse.logging_setup import get_logger

log = get_logger(__name__)
MANIFEST_NAME = "_generation_manifest.json"
CPC = {"paid_search": 1.15, "paid_social": 0.85, "display": 0.35, "affiliate": 0.60, "email": 0.05}
CTR = {
    "paid_search": 0.045,
    "paid_social": 0.012,
    "display": 0.0025,
    "affiliate": 0.02,
    "email": 0.03,
}


@dataclass
class GenerationSummary:
    """What a generation run produced."""

    batch_ids: list[str]
    rows: dict[str, int]
    files: int
    raw_bytes: int
    seconds: float
    workers: int
    chunk_seconds: list[float] = field(default_factory=list)
    fingerprint: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    @property
    def rows_per_second(self) -> float:
        return self.total_rows / self.seconds if self.seconds else 0.0


def dataset_fingerprint(params: dict[str, Any]) -> str:
    """Stable hash of generation parameters (identifies a reproducible dataset)."""
    blob = json.dumps(params, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _month_tasks(start: date, end: date) -> list[ChunkTask]:
    tasks: list[ChunkTask] = []
    d = date(start.year, start.month, 1)
    index = 0
    while d <= end:
        nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
        lo = max(d, start)
        hi = min(nxt, end + timedelta(days=1))
        tasks.append(ChunkTask(index, d, (lo - start).days, (hi - start).days))
        d, index = nxt, index + 1
    return tasks


def _products_files(
    state: SharedState, raw_dir: Path, rng: np.random.Generator
) -> tuple[dict[str, int], list[str]]:
    """Products land in the batch of their launch month (older launches: first batch)."""
    prods = state.products
    first_batch = batch_for(state.profile.start, 0)
    months = prods.launched_at.astype("datetime64[M]").astype(str)
    batch = np.where(months < first_batch, first_batch, months)
    rate = state.params.dq_issue_rate
    cols = {
        "product_id": prods.product_id,
        "product_name": prods.name,
        "category": dq.messy_categorical(
            rng, prods.category, rate * dq.ISSUE_SHARE["categorical"] * 2
        ),
        "subcategory": prods.subcategory,
        "brand": prods.brand,
        "list_price": prods.list_price,
        "unit_cost": prods.unit_cost,
        "launched_at": dq.iso_utc(prods.launched_at),
    }
    rows: dict[str, int] = {}
    files: list[str] = []
    for b in sorted(set(batch.tolist())):
        sel = batch == b
        rel, n = write_entity(raw_dir, "products", b, f"products_{b}", dq.take(cols, sel), "csv")
        if n:
            files.append(rel)
            rows["products"] = rows.get("products", 0) + n
    return rows, files


def _marketing_spend(
    state: SharedState, raw_dir: Path, revenue: np.ndarray, rng: np.random.Generator
) -> tuple[dict[str, int], list[str]]:
    """Daily spend per paid channel, sized from attributed revenue and a target ROAS."""
    prof = state.profile
    n_days = prof.n_days
    kernel = np.ones(14) / 14
    dates, channels, spend, clicks, impressions = [], [], [], [], []
    for ch in catalog.PAID_CHANNELS:
        src = catalog.TRAFFIC_SOURCES.index(ch)
        smooth = np.convolve(np.pad(revenue[:, src], (13, 0), mode="edge"), kernel, "valid")
        base = smooth / catalog.TARGET_ROAS[ch]
        floor = max(5.0, float(base.mean()) * 0.15)
        daily = np.maximum(base, floor) * rng.lognormal(0.0, 0.18, n_days)
        daily = np.round(daily, 2)
        clk = np.maximum(0, np.round(daily / CPC[ch] * rng.lognormal(0, 0.1, n_days))).astype(
            np.int64
        )
        imp = np.maximum(clk, np.round(clk / CTR[ch])).astype(np.int64)
        dates.append(prof.dates.astype(str).astype(object))
        channels.append(np.full(n_days, ch, dtype=object))
        spend.append(daily)
        clicks.append(clk)
        impressions.append(imp)
    cols = {
        "date": np.concatenate(dates),
        "channel": np.concatenate(channels),
        "spend": np.concatenate(spend),
        "impressions": np.concatenate(impressions),
        "clicks": np.concatenate(clicks),
        "currency": np.full(n_days * len(catalog.PAID_CHANNELS), "USD", dtype=object),
    }
    rate = state.params.dq_issue_rate
    cols["channel"] = dq.messy_categorical(
        rng, cols["channel"], rate * dq.ISSUE_SHARE["categorical"]
    )
    neg = dq.pick(rng, len(cols["spend"]), rate * 0.1)
    cols["spend"] = cols["spend"].copy()
    cols["spend"][neg] = -cols["spend"][neg]  # refund adjustments booked as negative spend
    order = np.lexsort((cols["channel"].astype(str), cols["date"].astype(str)))
    cols = dq.take(cols, order)
    month = np.array([d[:7] for d in cols["date"]], dtype=object)
    rows: dict[str, int] = {}
    files: list[str] = []
    for b in sorted(set(month.tolist())):
        part = dq.duplicate_rows(
            rng, dq.take(cols, month == b), rate * dq.ISSUE_SHARE["duplicates"]
        )
        rel, n = write_entity(raw_dir, "marketing_spend", b, f"marketing_spend_{b}", part, "jsonl")
        if n:
            files.append(rel)
            rows["marketing_spend"] = rows.get("marketing_spend", 0) + n
    return rows, files


def generate(
    settings: Settings, workers: int | None = None, raw_dir: Path | None = None
) -> GenerationSummary:
    """Generate the full synthetic dataset into ``raw_dir`` (default ``data/raw``).

    The output is a deterministic function of the generator settings (not of ``workers``):
    each monthly chunk uses its own seed derived from ``(seed, chunk index)``.
    """
    t0 = time.perf_counter()
    gen = settings.generator
    start, end = gen.start, gen.resolved_end()
    workers = workers or settings.resources.effective_workers()
    raw_dir = Path(raw_dir or settings.paths.raw_dir)
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)

    params = GeneratorParams(
        seed=gen.seed,
        n_customers=gen.customers,
        dq_issue_rate=gen.dq_issue_rate,
        late_arrival_rate=gen.late_arrival_rate,
        anonymous_sessions_per_customer=gen.anonymous_sessions_per_customer,
    )
    param_dict = {**params.__dict__, "start": start.isoformat(), "end": end.isoformat()}
    log.info(
        "generating %s customers, %s -> %s (seed=%s) with %d worker(s)",
        f"{gen.customers:,}",
        start,
        end,
        gen.seed,
        workers,
    )

    profile = build_daily_profile(start, end)
    products = build_products(np.random.default_rng([gen.seed, 1]), start, end)
    customers = build_customers(np.random.default_rng([gen.seed, 2]), gen.customers, profile)
    skeleton = simulate_orders(np.random.default_rng([gen.seed, 3]), customers, profile)
    log.info("skeleton: %s products, %s orders", len(products), f"{len(skeleton):,}")
    shared = SharedState(params, profile, products, customers, skeleton, str(raw_dir))
    tasks = _month_tasks(start, end)

    results: list[ChunkResult] = []
    if workers <= 1:
        for task in tasks:
            results.append(run_chunk(task, shared))
    else:
        ctx = mp.get_context("spawn")
        fd, state_path = tempfile.mkstemp(prefix="rlh_state_", suffix=".pkl")
        try:
            with os.fdopen(fd, "wb") as fh:
                pickle.dump(shared, fh, protocol=pickle.HIGHEST_PROTOCOL)
            with ProcessPoolExecutor(
                max_workers=min(workers, len(tasks)),
                mp_context=ctx,
                initializer=init_worker,
                initargs=(state_path,),
            ) as pool:
                # Later months are bigger (customer base grows): schedule them first.
                futures = [pool.submit(run_chunk, task) for task in reversed(tasks)]
                for fut in as_completed(futures):
                    results.append(fut.result())
        finally:
            Path(state_path).unlink(missing_ok=True)
    results.sort(key=lambda r: r.index)

    rows: dict[str, int] = {}
    files: list[str] = []
    revenue = np.zeros((profile.n_days, len(catalog.TRAFFIC_SOURCES)))
    for task, res in zip(tasks, results, strict=True):
        for k, v in res.rows.items():
            rows[k] = rows.get(k, 0) + v
        files += res.files
        if res.revenue_by_day_source is not None:
            revenue[task.day_lo : task.day_hi] += res.revenue_by_day_source

    for extra_rows, extra_files in (
        _products_files(shared, raw_dir, np.random.default_rng([gen.seed, 4])),
        _marketing_spend(shared, raw_dir, revenue, np.random.default_rng([gen.seed, 5])),
    ):
        for k, v in extra_rows.items():
            rows[k] = rows.get(k, 0) + v
        files += extra_files

    raw_bytes = sum((raw_dir / f).stat().st_size for f in files)
    batches = sorted({Path(f).parts[1] for f in files})
    summary = GenerationSummary(
        batch_ids=batches,
        rows=dict(sorted(rows.items())),
        files=len(files),
        raw_bytes=raw_bytes,
        seconds=time.perf_counter() - t0,
        workers=workers,
        chunk_seconds=[round(r.seconds, 3) for r in results],
        fingerprint=dataset_fingerprint(param_dict),
        params=param_dict,
    )
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fingerprint": summary.fingerprint,
        "params": param_dict,
        "rows": summary.rows,
        "files": sorted(files),
        "raw_bytes": raw_bytes,
        "batches": batches,
        "seconds": round(summary.seconds, 3),
        "workers": workers,
    }
    (raw_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info(
        "generated %s rows in %d files (%.1f MB) across %d batches in %.1fs (%s rows/s)",
        f"{summary.total_rows:,}",
        summary.files,
        raw_bytes / 1e6,
        len(batches),
        summary.seconds,
        f"{summary.rows_per_second:,.0f}",
    )
    return summary
