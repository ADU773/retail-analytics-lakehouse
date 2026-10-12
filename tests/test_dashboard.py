"""The Streamlit dashboard renders against a small fixture warehouse without exceptions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from retail_lakehouse.reporting import dashboard_data as dd

APP = Path(__file__).resolve().parents[1] / "app" / "dashboard.py"


def test_dashboard_queries(tiny_settings, tiny_run) -> None:
    con = dd.connect_readonly(tiny_settings.paths.warehouse_path)
    try:
        opts = dd.filter_options(con)
        full = dd.Filters(opts["min_date"], opts["max_date"])
        k = dd.kpis(con, full)
        assert k["orders"] > 0 and 0 < k["gross_margin_pct"] < 1
        region = dd.Filters(opts["min_date"], opts["max_date"], regions=(opts["regions"][0],))
        assert 0 < dd.kpis(con, region)["orders"] < k["orders"]
        assert dd.daily_revenue(con, full)["net_revenue"].sum() == pytest.approx(k["net_revenue"])
        assert not dd.revenue_by_category(con, full).empty
        assert not dd.channel_performance(con, full).empty
        assert set(dd.funnel(con, full)["device"]) >= {"desktop", "mobile"}
        assert not dd.rfm_segments(con, full).empty
        empty = dd.Filters(date(1990, 1, 1), date(1990, 1, 2))
        assert dd.kpis(con, empty)["orders"] == 0
    finally:
        con.close()


def test_dashboard_app_runs(tiny_settings, tiny_run, monkeypatch) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv(dd.WAREHOUSE_ENV_VAR, str(tiny_settings.paths.warehouse_path))
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [m.label for m in at.metric]
    assert "Net revenue" in labels and "Orders" in labels
    at.sidebar.multiselect[0].select(at.sidebar.multiselect[0].options[0]).run()
    assert not at.exception, [e.value for e in at.exception]


def test_dashboard_missing_warehouse(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv(dd.WAREHOUSE_ENV_VAR, str(tmp_path / "nope.duckdb"))
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert at.error and "Warehouse not found" in at.error[0].value
