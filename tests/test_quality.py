from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from retail_lakehouse.config import load_settings
from retail_lakehouse.quality.checks import compile_test, evaluate
from retail_lakehouse.quality.runner import LAYERS, load_layer
from retail_lakehouse.quality.spec import SpecError, parse_spec

AS_OF = date(2024, 6, 30)

SPEC = {
    "layer": "silver",
    "tables": {
        "silver.t": {
            "description": "test table",
            "tests": [
                {"row_count_min": 3},
                {"row_count_min": 100},
                {"freshness": {"column": "ts", "max_age_days": 2}},
                {"freshness": {"column": "ts", "max_age_days": 30, "severity": "warn"}},
                {"unique_combination": ["id", "status"]},
                {"sql": {"query": "SELECT * FROM silver.t WHERE amount > 1000"}},
            ],
            "columns": {
                "id": {"description": "key", "tests": ["not_null", "unique"]},
                "status": {"tests": [{"accepted_values": {"values": ["ok", "late"]}}]},
                "amount": {
                    "tests": [
                        {"range": {"min": 0, "max": 500}},
                        {"expression": {"expr": "amount <> 13", "severity": "warn"}},
                    ]
                },
                "parent_id": [{"relationships": {"to": "silver.p", "field": "id"}}],
                "email": [{"not_null": {"max_failure_rate": 0.5}}],
            },
        }
    },
}


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA silver")
    c.execute("CREATE TABLE silver.p AS SELECT * FROM (VALUES (1), (2)) v(id)")
    c.execute(
        """CREATE TABLE silver.t AS SELECT * FROM (VALUES
            (1, 'ok',   10.0, 1, 'a@x.io', TIMESTAMP '2024-06-20 10:00:00'),
            (2, 'late', 13.0, 2, NULL,     TIMESTAMP '2024-06-21 10:00:00'),
            (2, 'LATE', -5.0, 9, 'b@x.io', TIMESTAMP '2024-06-22 10:00:00'),
            (NULL, 'ok', 9.0, NULL, 'c@x.io', TIMESTAMP '2024-06-01 10:00:00')
        ) v(id, status, amount, parent_id, email, ts)"""
    )
    yield c
    c.close()


def _results(con: duckdb.DuckDBPyConnection) -> dict[str, tuple[int, int, str]]:
    spec = parse_spec(SPEC)
    out = {}
    for case in spec.tests:
        failures, total = con.execute(compile_test(case, AS_OF)).fetchone()
        out[case.test_id] = (failures, total, evaluate(case, failures, total))
    return out


def test_spec_parsing_collects_docs_and_tests() -> None:
    spec = parse_spec(SPEC)
    assert spec.layer == "silver"
    assert spec.tables["silver.t"].columns["id"] == "key"
    assert len(spec.tests) == 13


def test_each_test_type_counts_failures(con: duckdb.DuckDBPyConnection) -> None:
    r = _results(con)
    assert r["silver.t:row_count_min"][2] in {"pass", "fail"}
    assert r["silver.t.id:not_null"] == (1, 4, "fail")
    assert r["silver.t.id:unique"][0] == 2 and r["silver.t.id:unique"][2] == "fail"
    assert r["silver.t.status:accepted_values"][0] == 1
    assert r["silver.t.amount:range"][0] == 1
    assert r["silver.t.amount:expression[amount <> 13]"] == (1, 4, "warn")
    assert r["silver.t.parent_id:relationships[silver.p]"][0] == 1
    assert r["silver.t.email:not_null"] == (1, 4, "pass")  # 25% <= 50% tolerance
    assert r["silver.t:unique_combination"][0] == 0
    assert r["silver.t:sql"][2] == "pass"


def test_row_count_and_freshness(con: duckdb.DuckDBPyConnection) -> None:
    spec = parse_spec(SPEC)
    statuses = []
    for case in spec.tests:
        if case.test in ("row_count_min", "freshness"):
            failures, total = con.execute(compile_test(case, AS_OF)).fetchone()
            statuses.append((case.test, case.params, evaluate(case, failures, total)))
    assert statuses[0][2] == "pass"  # 4 rows >= 3
    assert statuses[1][2] == "fail"  # 4 rows < 100
    assert statuses[2][2] == "fail"  # newest row 8 days old > 2
    assert statuses[3][2] == "pass"  # within 30 days


@pytest.mark.parametrize(
    "bad",
    [
        {"tables": {}},
        {"layer": "x", "tables": {"a.b": {"tests": ["not_a_test"]}}},
        {"layer": "x", "tables": {"a.b": {"columns": {"c": [{"accepted_values": {}}]}}}},
        {"layer": "x", "tables": {"a.b": {"columns": {"c": [{"range": {}}]}}}},
        {"layer": "x", "tables": {"a.b": {"columns": {"c": [{"unique": {"severity": "fatal"}}]}}}},
    ],
)
def test_invalid_specs(bad: dict) -> None:
    with pytest.raises(SpecError):
        parse_spec(bad)


def test_shipped_specs_parse() -> None:
    settings = load_settings(environ={})
    counts = {layer: len(load_layer(settings, layer).tests) for layer in LAYERS}
    assert all(n > 10 for n in counts.values())
    severities = {c.severity for layer in LAYERS for c in load_layer(settings, layer).tests}
    assert severities == {"warn", "error"}


def test_failing_error_test_exits_non_zero(
    tiny_settings, tiny_run, tmp_path: Path, monkeypatch
) -> None:
    """An error-severity failure must make the CLI exit with a non-zero code."""
    from retail_lakehouse.cli import main

    dq = tmp_path / "dq"
    dq.mkdir()
    (dq / "gold.yaml").write_text(
        "layer: gold\ntables:\n  gold.fact_orders:\n    tests:\n      - row_count_min: 100000000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RLH__PATHS__DQ_DIR", str(dq))
    p = tiny_settings.paths
    common = [
        "--data-dir",
        str(p.data_dir),
        "--reports-dir",
        str(tmp_path / "rep"),
        "--memory-limit",
        "512MB",
        "--threads",
        "2",
    ]
    assert main([*common, "test", "--layer", "gold"]) == 1
    assert main([*common, "test", "--layer", "gold", "--no-fail"]) == 0
    assert (tmp_path / "rep" / "dq" / "dq_gold.md").exists()
