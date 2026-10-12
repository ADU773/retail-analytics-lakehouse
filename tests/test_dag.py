from __future__ import annotations

from pathlib import Path

import pytest

from retail_lakehouse.config import load_settings
from retail_lakehouse.transform.dag import CycleError, Dag, DagError
from retail_lakehouse.transform.models import (
    Model,
    ModelError,
    model_from_sql,
    parse_header,
    referenced_tables,
)
from retail_lakehouse.transform.runner import build_dag


def m(name: str, *deps: str, sql: str = "SELECT 1") -> Model:
    return Model(name=name, kind="sql", depends_on=deps, sql=sql)


def diamond() -> Dag:
    models = [
        m("silver.a", "bronze.raw"),
        m("silver.b", "silver.a"),
        m("silver.c", "silver.a"),
        m("gold.d", "silver.b", "silver.c"),
        m("gold.e", "silver.c"),
    ]
    return Dag({x.name: x for x in models}, sources={"bronze.raw"})


def test_parse_header_stops_at_sql_and_joins_repeats() -> None:
    text = (
        "-- description: Orders\n-- depends_on: bronze.orders\n-- depends_on: silver.customers\n"
        "\n-- materialized: view\nSELECT 1\n-- depends_on: ignored.table\n"
    )
    meta = parse_header(text)
    assert meta["depends_on"] == "bronze.orders, silver.customers"
    assert meta["materialized"] == "view"
    assert "ignored" not in meta["depends_on"]


def test_model_from_sql(tmp_path: Path) -> None:
    path = tmp_path / "orders.sql"
    path.write_text("-- depends_on: bronze.orders\nSELECT * FROM bronze.orders", encoding="utf-8")
    model = model_from_sql(path, "silver")
    assert model.name == "silver.orders"
    assert model.depends_on == ("bronze.orders",)
    path.write_text("-- materialized: ephemeral\nSELECT 1", encoding="utf-8")
    with pytest.raises(ModelError):
        model_from_sql(path, "silver")


def test_referenced_tables_ignores_comments_and_strings() -> None:
    sql = "SELECT 'gold.fake' FROM silver.orders -- gold.other\n JOIN /* meta.x */ gold.dim_date"
    assert referenced_tables(sql) == {"silver.orders", "gold.dim_date"}


def test_topological_order_is_deterministic() -> None:
    order = diamond().topological_order()
    assert order.index("silver.a") < order.index("silver.b") < order.index("gold.d")
    assert order.index("silver.c") < order.index("gold.e")
    assert order == diamond().topological_order()


def test_cycle_detection() -> None:
    models = [m("silver.a", "silver.c"), m("silver.b", "silver.a"), m("silver.c", "silver.b")]
    dag = Dag({x.name: x for x in models})
    with pytest.raises(CycleError) as exc:
        dag.topological_order()
    assert exc.value.cycle[0] == exc.value.cycle[-1]
    assert set(exc.value.cycle) == {"silver.a", "silver.b", "silver.c"}


def test_unknown_dependency_and_undeclared_reference() -> None:
    with pytest.raises(DagError, match="unknown"):
        Dag({"silver.a": m("silver.a", "silver.missing")})
    models = {
        "silver.a": m("silver.a"),
        "silver.b": m("silver.b", sql="SELECT * FROM silver.a"),  # not declared
    }
    with pytest.raises(DagError, match="does not declare"):
        Dag(models)


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (["silver.b"], ["silver.b"]),
        (["+gold.d"], ["silver.a", "silver.b", "silver.c", "gold.d"]),
        (["silver.c+"], ["silver.c", "gold.d", "gold.e"]),
        (["gold.*"], ["gold.d", "gold.e"]),
        (["e"], ["gold.e"]),
    ],
)
def test_selection(selector: list[str], expected: list[str]) -> None:
    assert diamond().select(selector) == sorted(expected, key=diamond().topological_order().index)


def test_selection_errors() -> None:
    with pytest.raises(DagError):
        diamond().select(["gold.nope"])
    with pytest.raises(DagError):
        diamond().select([])


def test_shipped_models_form_a_valid_dag() -> None:
    dag = build_dag(load_settings(environ={}))
    order = dag.topological_order()
    required = {
        "gold.dim_date",
        "gold.dim_customer",
        "gold.dim_product",
        "gold.fact_orders",
        "gold.fact_order_items",
        "gold.fact_sessions",
        "gold.mart_daily_kpis",
        "gold.mart_cohort_retention",
        "gold.mart_rfm",
        "gold.mart_customer_ltv",
        "gold.mart_market_basket",
        "gold.mart_channel_roas",
        "gold.mart_funnel",
    }
    assert required <= set(order)
    assert all(
        order.index(n) > order.index("silver.orders") for n in required if "fact_orders" in n
    )
    upstream = dag.select(["+gold.mart_rfm"])
    assert "silver.customers" in upstream
    assert "gold.mart_funnel" not in upstream
