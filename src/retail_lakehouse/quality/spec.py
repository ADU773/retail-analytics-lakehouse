"""Parse declarative data-quality YAML (dbt ``schema.yml`` style) into test cases.

Example::

    layer: silver
    tables:
      silver.orders:
        description: One row per order.
        tests:
          - row_count_min: 100
          - freshness: {column: order_ts, max_age_days: 2}
        columns:
          order_id:
            description: Natural key.
            tests: [not_null, unique]
          status:
            tests:
              - accepted_values: {values: [placed, shipped, delivered, cancelled]}
          customer_id:
            tests:
              - relationships: {to: silver.customers, field: customer_id, severity: warn}

Every test accepts ``severity`` (``error`` default, or ``warn``), an optional ``where``
filter and a tolerance: ``max_failures`` (row count) or ``max_failure_rate`` (fraction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEVERITIES = ("warn", "error")
COLUMN_TESTS = ("not_null", "unique", "accepted_values", "relationships", "range", "expression")
TABLE_TESTS = ("row_count_min", "freshness", "expression", "sql", "unique_combination")
COMMON_KEYS = {"severity", "where", "max_failures", "max_failure_rate", "description"}


class SpecError(ValueError):
    """Invalid data-quality specification."""


@dataclass
class TestCase:
    """One executable data-quality test."""

    layer: str
    table: str
    test: str
    column: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"
    where: str | None = None
    max_failures: int = 0
    max_failure_rate: float | None = None
    description: str = ""

    @property
    def test_id(self) -> str:
        col = f".{self.column}" if self.column else ""
        extra = ""
        if self.test == "expression":
            extra = f"[{self.params.get('expr', '')}]"
        elif self.test == "relationships":
            extra = f"[{self.params.get('to')}]"
        return f"{self.table}{col}:{self.test}{extra}"


@dataclass
class TableSpec:
    """Documentation for a table (used to build the data dictionary)."""

    name: str
    description: str = ""
    columns: dict[str, str] = field(default_factory=dict)


@dataclass
class LayerSpec:
    """All tests and docs declared for one layer."""

    layer: str
    tests: list[TestCase]
    tables: dict[str, TableSpec]


def _normalise(entry: Any) -> tuple[str, dict[str, Any]]:
    """``"unique"`` or ``{"range": {...}}`` or ``{"row_count_min": 10}`` -> (name, params)."""
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict) and len(entry) == 1:
        name, params = next(iter(entry.items()))
        if params is None:
            return name, {}
        if isinstance(params, dict):
            return name, dict(params)
        if name == "row_count_min":
            return name, {"min": params}
        if name == "unique_combination":
            return name, {"columns": list(params)}
        if name in ("expression", "sql"):
            return name, {"expr" if name == "expression" else "query": params}
        raise SpecError(f"test {name!r}: parameters must be a mapping")
    raise SpecError(f"cannot parse test entry {entry!r}")


def _make(layer: str, table: str, column: str | None, entry: Any) -> TestCase:
    name, params = _normalise(entry)
    allowed = COLUMN_TESTS if column else TABLE_TESTS
    if name not in allowed:
        where = f"column {table}.{column}" if column else f"table {table}"
        raise SpecError(f"unknown test {name!r} on {where}; expected one of {allowed}")
    severity = str(params.pop("severity", "error")).lower()
    if severity not in SEVERITIES:
        raise SpecError(f"{table}: severity must be warn or error (got {severity!r})")
    rate = params.pop("max_failure_rate", None)
    case = TestCase(
        layer=layer,
        table=table,
        test=name,
        column=column,
        severity=severity,
        where=params.pop("where", None),
        max_failures=int(params.pop("max_failures", 0)),
        max_failure_rate=float(rate) if rate is not None else None,
        description=str(params.pop("description", "")),
        params=params,
    )
    _validate_params(case)
    return case


def _validate_params(case: TestCase) -> None:
    p = case.params
    required = {
        "accepted_values": ["values"],
        "relationships": ["to", "field"],
        "row_count_min": ["min"],
        "freshness": ["column", "max_age_days"],
        "expression": ["expr"],
        "sql": ["query"],
        "unique_combination": ["columns"],
    }.get(case.test, [])
    missing = [k for k in required if k not in p]
    if missing:
        raise SpecError(f"{case.test_id}: missing parameter(s) {missing}")
    if case.test == "range" and "min" not in p and "max" not in p:
        raise SpecError(f"{case.test_id}: range needs min and/or max")


def parse_spec(data: dict[str, Any], source: str = "<dict>") -> LayerSpec:
    """Build a :class:`LayerSpec` from parsed YAML."""
    if not isinstance(data, dict) or "layer" not in data:
        raise SpecError(f"{source}: top-level 'layer' key is required")
    layer = str(data["layer"])
    tests: list[TestCase] = []
    tables: dict[str, TableSpec] = {}
    for table, body in (data.get("tables") or {}).items():
        body = body or {}
        doc = TableSpec(table, str(body.get("description", "")).strip())
        for entry in body.get("tests") or []:
            tests.append(_make(layer, table, None, entry))
        for column, col_body in (body.get("columns") or {}).items():
            col_body = col_body or {}
            if isinstance(col_body, list):  # shorthand: column: [not_null, unique]
                col_body = {"tests": col_body}
            doc.columns[column] = str(col_body.get("description", "")).strip()
            for entry in col_body.get("tests") or []:
                tests.append(_make(layer, table, column, entry))
        tables[table] = doc
    return LayerSpec(layer, tests, tables)


def load_spec(path: Path) -> LayerSpec:
    """Load one YAML file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parse_spec(data, source=path.name)
