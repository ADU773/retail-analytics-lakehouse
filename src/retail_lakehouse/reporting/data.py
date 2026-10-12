"""Pull everything the report needs from the gold layer into pandas DataFrames."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb
import pandas as pd

from retail_lakehouse.warehouse import table_exists


@dataclass
class ReportData:
    """DataFrames and scalars backing the report, images and insights."""

    as_of: date
    daily: pd.DataFrame
    monthly: pd.DataFrame
    cohorts: pd.DataFrame
    rfm: pd.DataFrame
    channels: pd.DataFrame
    funnel_device: pd.DataFrame
    funnel_source: pd.DataFrame
    pairs: pd.DataFrame
    ltv_by_rfm: pd.DataFrame
    backtest: pd.DataFrame
    returns_by_category: pd.DataFrame
    segments: pd.DataFrame
    quarantine: pd.DataFrame
    dq_summary: pd.DataFrame
    run_log: pd.DataFrame
    clv_params: dict[str, float]
    kpis: dict[str, float] = field(default_factory=dict)
    extra: dict[str, float] = field(default_factory=dict)


def _df(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


def load_report_data(con: duckdb.DuckDBPyConnection, as_of: date) -> ReportData:
    """Query gold/meta tables (read-only)."""
    daily = _df(con, "SELECT * FROM gold.mart_daily_kpis ORDER BY date")
    monthly = _df(
        con,
        """
        SELECT year_month, min(date) AS month_start, sum(net_revenue)::DOUBLE AS net_revenue,
               sum(orders) AS orders, sum(units) AS units, sum(gross_margin)::DOUBLE AS gross_margin,
               sum(sessions) AS sessions, sum(converted_sessions) AS converted_sessions,
               sum(new_customers) AS new_customers, sum(marketing_spend)::DOUBLE AS marketing_spend
        FROM gold.mart_daily_kpis GROUP BY year_month ORDER BY year_month
        """,
    )
    cohorts = _df(
        con,
        "SELECT * FROM gold.mart_cohort_retention ORDER BY cohort_month, months_since_first_order",
    )
    rfm = _df(
        con,
        """
        SELECT rfm_segment, count(*) AS customers, sum(monetary)::DOUBLE AS revenue,
               avg(recency_days) AS avg_recency_days, avg(frequency) AS avg_frequency,
               avg(monetary)::DOUBLE AS avg_monetary
        FROM gold.mart_rfm GROUP BY rfm_segment ORDER BY revenue DESC
        """,
    )
    channels = _df(
        con,
        """
        SELECT channel, channel_type, sum(spend)::DOUBLE AS spend, sum(clicks) AS clicks,
               sum(orders) AS orders, sum(attributed_revenue)::DOUBLE AS revenue,
               sum(attributed_margin)::DOUBLE AS margin, sum(new_customers) AS new_customers,
               sum(attributed_revenue)::DOUBLE / nullif(sum(spend)::DOUBLE, 0) AS roas,
               sum(attributed_margin)::DOUBLE / nullif(sum(spend)::DOUBLE, 0) AS poas,
               nullif(sum(spend)::DOUBLE, 0) / nullif(sum(new_customers), 0) AS cac
        FROM gold.mart_channel_roas GROUP BY channel, channel_type ORDER BY revenue DESC
        """,
    )
    funnel_device = _df(
        con,
        """
        SELECT device, sum(sessions) AS sessions, sum(product_view_sessions) AS product_view,
               sum(add_to_cart_sessions) AS add_to_cart, sum(checkout_sessions) AS checkout,
               sum(purchase_sessions) AS purchase
        FROM gold.mart_funnel GROUP BY device ORDER BY sessions DESC
        """,
    )
    funnel_source = _df(
        con,
        """
        SELECT traffic_source, sum(sessions) AS sessions, sum(purchase_sessions) AS purchase,
               sum(purchase_sessions) / sum(sessions) AS conversion_rate
        FROM gold.mart_funnel GROUP BY traffic_source ORDER BY conversion_rate DESC
        """,
    )
    pairs = _df(
        con, "SELECT * FROM gold.mart_market_basket ORDER BY lift DESC, orders_with_pair DESC"
    )
    ltv_by_rfm = _df(
        con,
        """
        SELECT r.rfm_segment, count(*) AS customers,
               avg(l.historical_clv) AS avg_historical_clv,
               avg(l.predicted_clv_12m) AS avg_predicted_clv_12m,
               sum(l.predicted_clv_12m) AS total_predicted_clv_12m,
               avg(l.p_alive) AS avg_p_alive
        FROM gold.mart_customer_ltv l JOIN gold.mart_rfm r USING (customer_id)
        GROUP BY r.rfm_segment ORDER BY avg_predicted_clv_12m DESC
        """,
    )
    backtest = (
        _df(con, "SELECT * FROM gold.mart_clv_backtest")
        if table_exists(con, "gold.mart_clv_backtest")
        else pd.DataFrame()
    )
    returns_by_category = _df(
        con,
        """
        SELECT category, sum(quantity) AS units, sum(returned_quantity) AS returned_units,
               sum(returned_quantity) / sum(quantity) AS return_rate,
               mode(return_reason) AS top_reason
        FROM gold.fact_order_items WHERE NOT is_cancelled
        GROUP BY category ORDER BY return_rate DESC
        """,
    )
    segments = _df(
        con,
        """
        SELECT customer_segment, count(*) AS orders, count(DISTINCT customer_id) AS customers,
               sum(net_revenue)::DOUBLE AS revenue, avg(net_revenue)::DOUBLE AS aov,
               count(*) / count(DISTINCT customer_id) AS orders_per_customer
        FROM gold.fact_orders WHERE NOT is_cancelled AND customer_key <> -1
        GROUP BY customer_segment ORDER BY revenue DESC
        """,
    )
    quarantine = _df(con, "SELECT * FROM gold.mart_quarantine_summary ORDER BY rejected_rows DESC")
    dq_summary = (
        _df(
            con,
            """
            SELECT layer, status, count(*) AS tests FROM meta.dq_results
            WHERE (layer, executed_at) IN (
                SELECT layer, max(executed_at) FROM meta.dq_results GROUP BY layer)
            GROUP BY layer, status ORDER BY layer, status
            """,
        )
        if table_exists(con, "meta.dq_results")
        else pd.DataFrame(columns=["layer", "status", "tests"])
    )
    run_log = (
        _df(
            con,
            """
            SELECT model, layer, kind, status, rows, quarantined_rows, seconds
            FROM meta.run_log
            QUALIFY row_number() OVER (PARTITION BY model ORDER BY started_at DESC) = 1
            ORDER BY layer DESC, seconds DESC
            """,
        )
        if table_exists(con, "meta.run_log")
        else pd.DataFrame()
    )
    params = (
        dict(
            con.execute(
                "SELECT param, value FROM meta.model_params WHERE model = 'mart_customer_ltv'"
            ).fetchall()
        )
        if table_exists(con, "meta.model_params")
        else {}
    )
    data = ReportData(
        as_of=as_of,
        daily=daily,
        monthly=monthly,
        cohorts=cohorts,
        rfm=rfm,
        channels=channels,
        funnel_device=funnel_device,
        funnel_source=funnel_source,
        pairs=pairs,
        ltv_by_rfm=ltv_by_rfm,
        backtest=backtest,
        returns_by_category=returns_by_category,
        segments=segments,
        quarantine=quarantine,
        dq_summary=dq_summary,
        run_log=run_log,
        clv_params=params,
    )
    data.kpis = _kpis(con)
    data.extra = _extra(con)
    return data


def _kpis(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    row = con.execute(
        """
        WITH o AS (
            SELECT * FROM gold.fact_orders WHERE NOT is_cancelled
        ), bounds AS (
            SELECT max(order_date) AS last_day FROM o
        ), periods AS (
            SELECT
                sum(net_revenue) FILTER (WHERE order_date > last_day - INTERVAL 365 DAY)::DOUBLE AS rev_ltm,
                sum(net_revenue) FILTER (WHERE order_date <= last_day - INTERVAL 365 DAY
                                         AND order_date > last_day - INTERVAL 730 DAY)::DOUBLE AS rev_prior,
                count(*) FILTER (WHERE order_date > last_day - INTERVAL 365 DAY) AS orders_ltm,
                count(*) FILTER (WHERE order_date <= last_day - INTERVAL 365 DAY
                                 AND order_date > last_day - INTERVAL 730 DAY) AS orders_prior
            FROM o, bounds
        ), repeaters AS (
            SELECT count(*) FILTER (WHERE n > 1) / count(*) AS repeat_rate
            FROM (SELECT customer_id, count(*) AS n FROM o GROUP BY 1)
        )
        SELECT
            (SELECT sum(net_revenue)::DOUBLE FROM o),
            (SELECT count(*) FROM o),
            (SELECT sum(net_revenue)::DOUBLE / count(*) FROM o),
            (SELECT sum(gross_margin)::DOUBLE / sum(net_revenue)::DOUBLE FROM o),
            (SELECT sum(returned_units) / sum(units) FROM o),
            (SELECT count(*) FILTER (WHERE reached_purchase) / count(*) FROM gold.fact_sessions),
            (SELECT count(DISTINCT customer_id) FROM o),
            (SELECT repeat_rate FROM repeaters),
            (SELECT sum(net_revenue)::DOUBLE FROM o) / (SELECT sum(spend)::DOUBLE FROM silver.marketing_spend),
            (SELECT avg(predicted_clv_12m) FROM gold.mart_customer_ltv),
            (SELECT count(*) FROM gold.fact_sessions),
            rev_ltm, rev_prior, orders_ltm, orders_prior,
            (SELECT sum(spend)::DOUBLE FROM silver.marketing_spend),
            (SELECT sum(units) FROM o)
        FROM periods
        """
    ).fetchone()
    keys = [
        "net_revenue",
        "orders",
        "aov",
        "gross_margin_pct",
        "return_rate",
        "conversion_rate",
        "customers",
        "repeat_rate",
        "mer",
        "avg_predicted_clv_12m",
        "sessions",
        "revenue_ltm",
        "revenue_prior",
        "orders_ltm",
        "orders_prior",
        "marketing_spend",
        "units",
    ]
    out = {k: float(v) if v is not None else float("nan") for k, v in zip(keys, row, strict=True)}
    if out["revenue_prior"] and out["revenue_prior"] == out["revenue_prior"]:
        out["revenue_yoy"] = out["revenue_ltm"] / out["revenue_prior"] - 1
        out["orders_yoy"] = out["orders_ltm"] / out["orders_prior"] - 1
    return out


def _extra(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """Assorted scalars used by the insights."""
    row = con.execute(
        """
        SELECT
            (SELECT count(*) FROM silver.orders WHERE order_ts_format <> 'iso_utc'),
            (SELECT count(*) FROM silver.orders),
            (SELECT count(*) FROM silver.orders WHERE is_late_arriving),
            (SELECT count(*) FROM silver.web_events WHERE is_late_arriving),
            (SELECT sum(rejected_rows) FROM gold.mart_quarantine_summary),
            (SELECT sum(bronze_rows) FROM (SELECT DISTINCT entity, bronze_rows FROM gold.mart_quarantine_summary)),
            (SELECT count(*) FROM gold.dim_customer WHERE version > 1),
            (SELECT count(DISTINCT customer_id) FROM gold.dim_customer WHERE version > 1),
            (SELECT count(*) FROM gold.dim_customer WHERE customer_key > 0)
        """
    ).fetchone()
    keys = [
        "nonstandard_ts",
        "silver_orders",
        "late_orders",
        "late_events",
        "quarantined_rows",
        "bronze_rows_with_rejects",
        "scd2_extra_versions",
        "scd2_customers",
        "dim_customer_rows",
    ]
    return {k: float(v or 0) for k, v in zip(keys, row, strict=True)}
