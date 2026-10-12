"""Compile :class:`TestCase` objects into SQL that measures failures.

Every compiled query returns exactly one row ``(failures, total)``:

* row-level tests: ``failures`` = rows violating the rule, ``total`` = rows checked;
* ``row_count_min``: ``failures`` = 1 when the table is too small, ``total`` = row count;
* ``freshness``: ``failures`` = 1 when stale, ``total`` = age in days of the newest row.
"""

from __future__ import annotations

from datetime import date

from retail_lakehouse.quality.spec import TestCase


def ident(name: str) -> str:
    """Quote a column identifier (handles reserved words such as ``date``)."""
    return '"' + name.replace('"', '""') + '"'


def literal(value: object) -> str:
    """SQL literal for YAML scalars."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _filter(case: TestCase, *conds: str) -> str:
    parts = [c for c in (case.where, *conds) if c]
    return ("WHERE " + " AND ".join(f"({p})" for p in parts)) if parts else ""


def compile_test(case: TestCase, as_of: date) -> str:
    """SQL returning ``(failures, total)`` for one test."""
    t = case.table
    p = case.params
    col = ident(case.column) if case.column else None
    base = _filter(case)

    if case.test == "not_null":
        return f"SELECT count(*) FILTER (WHERE {col} IS NULL), count(*) FROM {t} {base}"
    if case.test == "unique":
        return (
            f"SELECT coalesce(sum(n), 0), (SELECT count(*) FROM {t} {base}) FROM ("
            f"SELECT count(*) AS n FROM {t} {_filter(case, f'{col} IS NOT NULL')} "
            f"GROUP BY {col} HAVING count(*) > 1)"
        )
    if case.test == "unique_combination":
        cols = ", ".join(ident(c) for c in p["columns"])
        return (
            f"SELECT coalesce(sum(n), 0), (SELECT count(*) FROM {t} {base}) FROM ("
            f"SELECT count(*) AS n FROM {t} {base} GROUP BY {cols} HAVING count(*) > 1)"
        )
    if case.test == "accepted_values":
        values = ", ".join(literal(v) for v in p["values"])
        null_rule = "" if p.get("allow_null", True) else f" OR {col} IS NULL"
        return (
            f"SELECT count(*) FILTER (WHERE {col} NOT IN ({values}){null_rule}), count(*) "
            f"FROM {t} {base}"
        )
    if case.test == "relationships":
        parent, field = p["to"], ident(p["field"])
        return (
            f"SELECT count(*) FILTER (WHERE {col} IS NOT NULL AND {col} NOT IN "
            f"(SELECT {field} FROM {parent} WHERE {field} IS NOT NULL)), count(*) "
            f"FROM {t} {base}"
        )
    if case.test == "range":
        conds = []
        if "min" in p:
            conds.append(f"{col} < {literal(p['min'])}")
        if "max" in p:
            conds.append(f"{col} > {literal(p['max'])}")
        return f"SELECT count(*) FILTER (WHERE {' OR '.join(conds)}), count(*) FROM {t} {base}"
    if case.test == "expression":
        return f"SELECT count(*) FILTER (WHERE ({p['expr']}) IS NOT TRUE), count(*) FROM {t} {base}"
    if case.test == "row_count_min":
        return (
            f"SELECT CASE WHEN count(*) < {int(p['min'])} THEN 1 ELSE 0 END, count(*) "
            f"FROM {t} {base}"
        )
    if case.test == "freshness":
        c = ident(p["column"])
        age = f"date_diff('day', CAST(max({c}) AS DATE), DATE '{as_of.isoformat()}')"
        return (
            f"SELECT CASE WHEN max({c}) IS NULL OR {age} > {int(p['max_age_days'])} "
            f"THEN 1 ELSE 0 END, coalesce({age}, -1) FROM {t} {base}"
        )
    if case.test == "sql":
        return f"SELECT count(*), count(*) FROM ({p['query']}) AS failing_rows"
    raise ValueError(f"unsupported test {case.test}")  # pragma: no cover - guarded by spec


def evaluate(case: TestCase, failures: int, total: int) -> str:
    """Map raw counts to ``pass`` / ``warn`` / ``fail`` using tolerance and severity."""
    if case.test in ("row_count_min", "freshness"):
        breached = failures > 0
    elif case.max_failure_rate is not None:
        breached = total > 0 and failures / total > case.max_failure_rate
    else:
        breached = failures > case.max_failures
    if not breached:
        return "pass"
    return "warn" if case.severity == "warn" else "fail"
