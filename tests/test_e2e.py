"""End-to-end: the tiny-scale pipeline builds every layer, passes DQ and publishes the report."""

from __future__ import annotations

import json

import duckdb
import pytest

from retail_lakehouse.bronze.ingest import ingest
from retail_lakehouse.cli import main
from retail_lakehouse.transform.runner import build_dag, run_models

pytestmark = pytest.mark.e2e

RFM_SEGMENTS = {
    "Champions",
    "Loyal Customers",
    "Potential Loyalists",
    "New Customers",
    "Promising",
    "Need Attention",
    "About to Sleep",
    "At Risk",
    "Cannot Lose Them",
    "Hibernating",
}


@pytest.fixture
def wh(tiny_settings, tiny_run):
    con = duckdb.connect(str(tiny_settings.paths.warehouse_path), read_only=True)
    yield con
    con.close()


def test_run_all_steps_and_summary(tiny_settings, tiny_run) -> None:
    steps = [s.step for s in tiny_run.steps]
    assert steps == [
        "generate",
        "ingest",
        "test_bronze",
        "transform_silver",
        "test_silver",
        "transform_gold",
        "test_gold",
        "compact",
        "report",
    ]
    summary = json.loads((tiny_settings.paths.reports_dir / "run_summary.json").read_text("utf-8"))
    assert summary["system"]["host"] == "host-01"
    assert summary["dq"]["gold"]["fail"] == 0
    assert summary["storage_bytes"]["bronze"] > 0


def test_every_model_built_and_logged(tiny_settings, wh) -> None:
    models = set(build_dag(tiny_settings).models)
    logged = dict(
        wh.execute(
            "SELECT model, arg_max(status, started_at) FROM meta.run_log GROUP BY model"
        ).fetchall()
    )
    assert models <= set(logged)
    assert set(logged.values()) == {"success"}
    for name in models:
        assert wh.execute(f"SELECT count(*) FROM {name}").fetchone()[0] > 0, name


def test_quarantine_captures_rejections(wh) -> None:
    rows = wh.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'quarantine'"
    ).fetchall()
    assert len(rows) >= 6
    reasons = wh.execute(
        "SELECT DISTINCT rejection_reason FROM quarantine.orders WHERE rejection_reason IS NOT NULL"
    ).fetchall()
    assert reasons
    # silver is clean: no duplicate keys, no non-positive quantities
    assert (
        wh.execute("SELECT count(*) - count(DISTINCT order_id) FROM silver.orders").fetchone()[0]
        == 0
    )
    assert (
        wh.execute("SELECT count(*) FROM silver.order_items WHERE quantity <= 0").fetchone()[0] == 0
    )


def test_gold_semantics(wh) -> None:
    # SCD2: some customers changed segment/region and have multiple versions, one current.
    multi = wh.execute(
        "SELECT count(*) FROM (SELECT customer_id FROM gold.dim_customer WHERE customer_key > 0 "
        "GROUP BY 1 HAVING count(*) > 1)"
    ).fetchone()[0]
    assert multi > 0
    assert (
        wh.execute(
            "SELECT count(*) FROM (SELECT customer_id FROM gold.dim_customer WHERE is_current "
            "GROUP BY 1 HAVING count(*) > 1)"
        ).fetchone()[0]
        == 0
    )
    segments = {
        r[0] for r in wh.execute("SELECT DISTINCT rfm_segment FROM gold.mart_rfm").fetchall()
    }
    assert segments <= RFM_SEGMENTS and len(segments) >= 4
    lift = wh.execute(
        "SELECT min(lift), min(support), max(confidence_a_to_b) FROM gold.mart_market_basket"
    ).fetchone()
    assert lift[0] > 0 and lift[1] > 0 and lift[2] <= 1
    kpi = wh.execute(
        "SELECT sum(net_revenue), avg(gross_margin_pct), max(conversion_rate) FROM gold.mart_daily_kpis"
    ).fetchone()
    orders_rev = wh.execute(
        "SELECT sum(net_revenue) FROM gold.fact_orders WHERE NOT is_cancelled"
    ).fetchone()[0]
    assert kpi[0] == pytest.approx(orders_rev, rel=1e-6)
    assert 0 < kpi[1] < 1 and 0 < kpi[2] <= 1
    ltv = wh.execute(
        "SELECT min(p_alive), max(p_alive), sum(predicted_clv_12m) FROM gold.mart_customer_ltv"
    ).fetchone()
    assert 0 <= ltv[0] <= ltv[1] <= 1 and ltv[2] > 0
    retention = wh.execute(
        "SELECT max(retention_rate) FROM gold.mart_cohort_retention WHERE months_since_first_order = 0"
    ).fetchone()[0]
    assert retention == pytest.approx(1.0)


def test_reingest_is_noop_and_select_runs_subgraph(tiny_settings, tiny_run) -> None:
    again = ingest(tiny_settings)
    assert again.new_files == 0
    result = run_models(tiny_settings, select=["+gold.mart_rfm"])
    names = [r.name for r in result.results]
    assert names[-1] == "gold.mart_rfm"
    assert "silver.customers" in names and "gold.mart_funnel" not in names
    downstream = run_models(tiny_settings, select=["gold.fact_sessions+"])
    assert {r.name for r in downstream.results} >= {"gold.fact_sessions", "gold.mart_funnel"}


def test_report_outputs(tiny_settings, tiny_run) -> None:
    docs = tiny_settings.paths.docs_dir
    html = (docs / "index.html").read_text(encoding="utf-8")
    assert "cdn.plot.ly" in html and len(html) < 2_000_000
    assert "Aurora Goods Co." in html
    for name in (
        "revenue_trend",
        "cohort_retention",
        "rfm_segments",
        "channel_roas",
        "funnel_by_device",
        "top_product_pairs",
    ):
        assert (docs / "images" / f"{name}.png").stat().st_size > 5_000
    insights = (docs / "insights.md").read_text(encoding="utf-8")
    assert insights.count("\n## ") >= 6
    # privacy: no absolute local paths leak into published artifacts
    root = str(tiny_settings.paths.data_dir.parent)
    for f in docs.rglob("*"):
        if f.suffix in {".html", ".md", ".json"}:
            text = f.read_text(encoding="utf-8")
            assert root not in text and root.replace("\\", "/") not in text, f.name
    (html + insights).encode("ascii", "ignore")  # renders without crashing


def test_cli_transform_select_and_lineage(tiny_settings, tiny_run, capsys) -> None:
    p = tiny_settings.paths
    common = [
        "--data-dir",
        str(p.data_dir),
        "--reports-dir",
        str(p.reports_dir),
        "--threads",
        "2",
        "--memory-limit",
        "512MB",
    ]
    assert main([*common, "transform", "--select", "gold.mart_funnel"]) == 0
    assert main([*common, "transform", "--select", "gold.no_such_model"]) == 1
    assert main(["lineage", "--format", "mermaid", "--select", "+gold.mart_rfm"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("flowchart LR") and "gold_mart_rfm" in out
