from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np

from retail_lakehouse.bronze.contracts import CONTRACTS
from retail_lakehouse.generator import quality as dq
from retail_lakehouse.generator.calendar import (
    black_friday,
    build_daily_profile,
    build_promotions,
    cyber_monday,
)
from retail_lakehouse.generator.runner import MANIFEST_NAME, generate


def _digest(raw_dir: Path) -> dict[str, str]:
    return {
        p.relative_to(raw_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(raw_dir.rglob("*"))
        if p.is_file() and p.name != MANIFEST_NAME
    }


def test_retail_calendar() -> None:
    assert black_friday(2024) == date(2024, 11, 29)
    assert black_friday(2025) == date(2025, 11, 28)
    assert cyber_monday(2025) == date(2025, 12, 1)
    promos = build_promotions(date(2024, 1, 1), date(2024, 12, 31))
    assert promos and all(p.code and p.start <= p.end for p in promos)
    assert any(p.start <= black_friday(2024) <= p.end for p in promos)


def test_daily_profile_has_black_friday_peak() -> None:
    profile = build_daily_profile(date(2024, 1, 1), date(2024, 12, 31))
    assert profile.n_days == 366
    demand = np.asarray(profile.demand)
    bf = (black_friday(2024) - date(2024, 1, 1)).days
    assert demand[bf] > 2 * np.median(demand)


def test_dq_injectors() -> None:
    rng = np.random.default_rng(0)
    assert len(dq.pick(rng, 10_000, 0.02)) in range(150, 260)
    assert len(dq.pick(rng, 0, 0.5)) == 0
    qty = dq.bad_quantities(rng, np.ones(5_000, dtype=int), 0.02)
    assert 0 < (qty <= 0).sum() < 200
    cats = dq.messy_categorical(rng, np.array(["mobile"] * 2_000, dtype=object), 0.05)
    messy = [c for c in cats if c != "mobile"]
    assert messy and all(c.strip().lower() == "mobile" for c in messy)
    emails = dq.invalid_emails(rng, np.array(["a@b.io"] * 1_000, dtype=object), 0.05)
    assert 0 < sum(e != "a@b.io" for e in emails) < 120
    cols = {"id": np.arange(1_000)}
    dup = dq.duplicate_rows(rng, cols, 0.02)
    assert len(dup["id"]) > 1_000 and len(np.unique(dup["id"])) == 1_000


def test_generation_is_reproducible_across_worker_counts(small_settings, tmp_path: Path) -> None:
    a = generate(small_settings, workers=1, raw_dir=tmp_path / "a")
    b = generate(small_settings, workers=2, raw_dir=tmp_path / "b")
    assert a.rows == b.rows and a.fingerprint == b.fingerprint
    assert _digest(tmp_path / "a") == _digest(tmp_path / "b")


def test_landing_zone_layout_and_contracts(small_settings) -> None:
    summary = generate(small_settings, workers=1)
    raw = small_settings.paths.raw_dir
    assert set(summary.rows) == set(CONTRACTS)
    assert all(summary.rows[name] > 0 for name in CONTRACTS)
    manifest = json.loads((raw / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["fingerprint"] == summary.fingerprint
    for name, contract in CONTRACTS.items():
        files = sorted((raw / name).glob(f"*/*.{contract.extension}"))
        assert files, name  # data/raw/<entity>/<batch>/<file>
        first = files[0]
        if contract.extension == "csv":
            with first.open(encoding="utf-8", newline="") as fh:
                header = next(csv.reader(fh))
        else:
            with first.open(encoding="utf-8") as fh:
                header = list(json.loads(fh.readline()))
        assert set(contract.columns) <= set(header), name
    # late-arriving data: more than one batch exists
    assert len(summary.batch_ids) > 1
