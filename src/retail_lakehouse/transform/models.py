"""Model definitions: SQL files with a comment header, plus registered Python models.

A SQL model is a ``.sql`` file whose leading comment block declares metadata::

    -- description: Typed, de-duplicated orders (one row per order_id).
    -- depends_on: bronze.orders, silver.customers
    -- materialized: table            (table | view)
    -- quarantine: quarantine.orders  (optional: split rows on ``rejection_reason``)

The model name defaults to ``<folder>.<file stem>`` (e.g. ``silver/orders.sql`` ->
``silver.orders``) and becomes the DuckDB ``schema.table`` it materialises.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LAYERS = ("silver", "gold")
MATERIALIZATIONS = ("table", "view")
HEADER_RE = re.compile(r"^\s*--\s*([a-z_]+)\s*:\s*(.*?)\s*$")
REF_RE = re.compile(r"\b(bronze|silver|gold|quarantine|meta)\.([a-z_][a-z0-9_]*)\b", re.IGNORECASE)
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")


class ModelError(ValueError):
    """Invalid model definition."""


@dataclass
class Model:
    """A node of the transformation DAG."""

    name: str
    kind: str  # "sql" | "python"
    depends_on: tuple[str, ...] = ()
    materialized: str = "table"
    quarantine: str | None = None
    description: str = ""
    sql: str | None = None
    path: Path | None = None
    func: Callable[..., Any] | None = None
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def layer(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def table(self) -> str:
        return self.name.split(".", 1)[1]


def parse_header(text: str) -> dict[str, str]:
    """Parse the leading ``-- key: value`` comment block of a SQL file.

    Parsing stops at the first line that is neither blank nor a comment. Repeated keys are
    joined (``depends_on`` with commas, everything else with spaces).
    """
    meta: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            break
        match = HEADER_RE.match(stripped)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key in meta:
            sep = ", " if key == "depends_on" else " "
            meta[key] = f"{meta[key]}{sep}{value}"
        else:
            meta[key] = value
    return meta


def split_list(value: str | None) -> tuple[str, ...]:
    """Split a comma/space separated header value into a tuple of names."""
    if not value:
        return ()
    return tuple(p for p in re.split(r"[,\s]+", value.strip()) if p)


def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` and ``/* */`` comments and string literals (for reference scanning)."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def referenced_tables(sql: str) -> set[str]:
    """Qualified ``schema.table`` names referenced in a SQL body."""
    return {f"{s.lower()}.{t.lower()}" for s, t in REF_RE.findall(strip_sql_comments(sql))}


def model_from_sql(path: Path, layer: str) -> Model:
    """Build a :class:`Model` from a ``.sql`` file."""
    text = path.read_text(encoding="utf-8")
    meta = parse_header(text)
    name = meta.get("name", f"{layer}.{path.stem}").lower()
    if not NAME_RE.match(name):
        raise ModelError(f"{path.name}: invalid model name {name!r} (expected schema.table)")
    materialized = meta.get("materialized", "table").lower()
    if materialized not in MATERIALIZATIONS:
        raise ModelError(f"{name}: materialized must be one of {MATERIALIZATIONS}")
    quarantine = meta.get("quarantine") or None
    if quarantine and materialized != "table":
        raise ModelError(f"{name}: quarantine requires materialized: table")
    return Model(
        name=name,
        kind="sql",
        depends_on=tuple(d.lower() for d in split_list(meta.get("depends_on"))),
        materialized=materialized,
        quarantine=quarantine.lower() if quarantine else None,
        description=meta.get("description", ""),
        sql=text,
        path=path,
        meta=meta,
    )


def load_sql_models(sql_dir: Path, layers: tuple[str, ...] = LAYERS) -> dict[str, Model]:
    """Discover ``<sql_dir>/<layer>/*.sql`` models."""
    models: dict[str, Model] = {}
    for layer in layers:
        folder = sql_dir / layer
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.sql")):
            model = model_from_sql(path, layer)
            if model.name in models:
                raise ModelError(f"duplicate model name {model.name}")
            models[model.name] = model
    return models
