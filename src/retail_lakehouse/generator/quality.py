"""Injection of realistic data-quality problems into generated columns.

Every injector works on columnar numpy data, touches roughly ``rate`` of the rows and
returns new arrays (inputs are never mutated). The silver layer is expected to repair or
quarantine every problem introduced here; the DQ tests on bronze surface them as warnings.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

Columns = dict[str, np.ndarray]

# Relative weight of each problem type, multiplied by the configured ``dq_issue_rate``.
ISSUE_SHARE = {
    "duplicates": 0.35,
    "categorical": 0.45,
    "timestamps": 0.60,
    "emails": 0.80,
    "quantities": 0.30,
    "orphans": 0.15,
}


def pick(rng: np.random.Generator, n: int, rate: float) -> np.ndarray:
    """Indices of ~``rate * n`` randomly chosen rows (binomial count, no replacement)."""
    if n == 0 or rate <= 0:
        return np.empty(0, dtype=np.int64)
    k = min(n, int(rng.binomial(n, min(1.0, rate))))
    return np.sort(rng.choice(n, size=k, replace=False)) if k else np.empty(0, dtype=np.int64)


def iso_utc(ts: np.ndarray) -> np.ndarray:
    """Canonical ``YYYY-MM-DDTHH:MM:SSZ`` strings for datetime64[s] values."""
    if len(ts) == 0:
        return np.empty(0, dtype=object)
    return np.char.add(np.datetime_as_string(ts.astype("datetime64[s]"), unit="s"), "Z").astype(
        object
    )


def messy_timestamps(
    rng: np.random.Generator,
    ts: np.ndarray,
    rate: float,
    utc_offset_hours: np.ndarray | None = None,
) -> np.ndarray:
    """Render timestamps as ISO-8601 UTC, with ~``rate`` in inconsistent formats/time zones.

    Variants: local time with an explicit offset (``2024-03-05 09:22:10-05:00``), US
    month/day format without zone (``03/05/2024 14:22:10``) and Unix epoch seconds.
    """
    out = iso_utc(ts)
    idx = pick(rng, len(ts), rate)
    if not len(idx):
        return out
    kinds = rng.integers(0, 3, size=len(idx))
    offsets = (
        utc_offset_hours[idx]
        if utc_offset_hours is not None
        else rng.choice([-8, -5, 1, 9], size=len(idx))
    )
    for j, i in enumerate(idx):
        value = ts[i].astype("datetime64[s]")
        if kinds[j] == 0:
            off = int(offsets[j])
            local = value + np.timedelta64(off * 3600, "s")
            sign = "+" if off >= 0 else "-"
            text = str(local).replace("T", " ")
            out[i] = f"{text}{sign}{abs(off):02d}:00"
        elif kinds[j] == 1:
            text = str(value)
            out[i] = f"{text[5:7]}/{text[8:10]}/{text[0:4]} {text[11:19]}"
        else:
            out[i] = str(int(value.astype(np.int64)))
    return out


def messy_categorical(rng: np.random.Generator, values: np.ndarray, rate: float) -> np.ndarray:
    """Upper/title-case and whitespace-pad ~``rate`` of categorical values."""
    out = values.astype(object).copy()
    for i in pick(rng, len(values), rate):
        v = out[i]
        if v is None:
            continue
        kind = int(rng.integers(0, 5))
        out[i] = [
            str(v).upper(),
            str(v).title(),
            f" {v}",
            f"{v}  ",
            f"  {str(v).upper()} ",
        ][kind]
    return out


def invalid_emails(rng: np.random.Generator, emails: np.ndarray, rate: float) -> np.ndarray:
    """Null out or corrupt ~``rate`` of e-mail addresses."""
    out = emails.astype(object).copy()
    for i in pick(rng, len(emails), rate):
        kind = int(rng.integers(0, 5))
        v = str(out[i])
        out[i] = [None, "", "n/a", v.replace("@", ""), v.replace("@", "@@")][kind]
    return out


def bad_quantities(rng: np.random.Generator, qty: np.ndarray, rate: float) -> np.ndarray:
    """Set ~``rate`` of quantities to zero or a negative number."""
    out = qty.copy()
    idx = pick(rng, len(qty), rate)
    out[idx] = rng.choice([0, -1, -2], size=len(idx))
    return out


def orphan_keys(
    rng: np.random.Generator, keys: np.ndarray, rate: float, prefix: str, width: int
) -> np.ndarray:
    """Replace ~``rate`` of foreign keys with ids that do not exist upstream."""
    out = keys.astype(object).copy()
    idx = pick(rng, len(keys), rate)
    for i in idx:
        out[i] = f"{prefix}9{int(rng.integers(0, 10 ** (width - 1))):0{width - 1}d}"
    return out


def duplicate_rows(rng: np.random.Generator, cols: Columns, rate: float) -> Columns:
    """Append exact copies of ~``rate`` of the rows (re-delivered records)."""
    n = len(next(iter(cols.values()))) if cols else 0
    idx = pick(rng, n, rate)
    if not len(idx):
        return cols
    return {k: np.concatenate([v, v[idx]]) for k, v in cols.items()}


def take(cols: Columns, mask_or_idx: np.ndarray) -> Columns:
    """Row subset of a column dict."""
    return {k: v[mask_or_idx] for k, v in cols.items()}


def concat(parts: Sequence[Columns]) -> Columns:
    """Concatenate column dicts with identical keys."""
    parts = [p for p in parts if p and len(next(iter(p.values())))]
    if not parts:
        return {}
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
