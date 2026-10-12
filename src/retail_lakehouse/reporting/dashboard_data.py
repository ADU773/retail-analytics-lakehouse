"""Filterable queries behind the interactive Streamlit dashboard (``app/dashboard.py``).

Kept free of Streamlit imports so the SQL can be unit-tested against any gold database.
Filters apply consistently:

* **date range** - order date / session date / spend month;
* **region** - the customer's region at order time (sessions: geo of the visit);
* **channel** - last-touch attributed channel for orders, traffic source for sessions and
  the media channel for spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_WAREHOUSE = Path("data") / "warehouse" / "lakehouse.duckdb"
WAREHOUSE_ENV_VAR = "RLH_WAREHOUSE"


@dataclass(frozen=True)
class Filters:
    """Dashboard filter state (empty lists mean "all")."""

    start: date
    end: date
    regions: tuple[str, ...] = field(default_factory=tuple)
    channels: tuple[str, ...] = field(default_factory=tuple)


def connect_readonly(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Read-only connection to a gold warehouse file (raises FileNotFoundError if missing)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"warehouse not found at {p} - run `retail-lakehouse run-all`")
    return duckdb.connect(str(p), read_only=True)


def filter_options(con: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """Available values for the sidebar widgets."""
    lo, hi = con.execute("SELECT min(order_date), max(order_date) FROM gold.fact_orders").fetchone()
    regions = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT customer_region FROM gold.fact_orders "
            "WHERE customer_region IS NOT NULL ORDER BY 1"
        ).fetchall()
    ]
    channels = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT attributed_channel FROM gold.fact_orders "
            "WHERE attributed_channel IS NOT NULL ORDER BY 1"
        ).fetchall()
    ]
    return {"min_date": lo, "max_date": hi, "regions": regions, "channels": channels}


def _where(
    f: Filters, date_col: str, region_col: str | None, channel_col: str | None
) -> tuple[str, list]:
    clauses = [f"{date_col} BETWEEN ? AND ?"]
    params: list = [f.start, f.end]
    if f.regions and region_col:
        clauses.append(f"list_contains(?::VARCHAR[], {region_col})")
        params.append(list(f.regions))
    if f.channels and channel_col:
        clauses.append(f"list_contains(?::VARCHAR[], {channel_col})")
        params.append(list(f.channels))
    return " AND ".join(clauses), params


def kpis(con: duckdb.DuckDBPyConnection, f: Filters) -> dict[str, float]:
    """Headline KPIs for the filtered orders (cancelled orders excluded)."""
    where, params = _where(f, "order_date", "customer_region", "attributed_channel")
    row = con.execute(
        f"""
        SELECT coalesce(sum(net_revenue), 0)::DOUBLE, count(*),
               coalesce(sum(units), 0), coalesce(sum(gross_margin), 0)::DOUBLE,
               coalesce(sum(returned_units), 0), count(DISTINCT customer_id)
        FROM gold.fact_orders WHERE NOT is_cancelled AND {where}
        """,
        params,
    ).fetchone()
    revenue, orders, units, margin, returned, customers = row
    return {
        "net_revenue": float(revenue),
        "orders": int(orders),
        "aov": float(revenue) / orders if orders else 0.0,
        "units": int(units),
        "gross_margin_pct": float(margin) / float(revenue) if revenue else 0.0,
        "return_rate": float(returned) / float(units) if units else 0.0,
        "customers": int(customers),
    }


def daily_revenue(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    """Daily net revenue and orders with a 7-day moving average."""
    where, params = _where(f, "order_date", "customer_region", "attributed_channel")
    return con.execute(
        f"""
        SELECT order_date AS date, sum(net_revenue)::DOUBLE AS net_revenue, count(*) AS orders,
               avg(sum(net_revenue)::DOUBLE) OVER (
                   ORDER BY order_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS revenue_7d
        FROM gold.fact_orders WHERE NOT is_cancelled AND {where}
        GROUP BY order_date ORDER BY order_date
        """,
        params,
    ).df()


def revenue_by_category(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    """Net revenue, margin and return rate per product category."""
    where, params = _where(f, "i.order_date", "o.customer_region", "o.attributed_channel")
    return con.execute(
        f"""
        SELECT i.category, sum(i.net_amount)::DOUBLE AS net_revenue,
               sum(i.gross_margin)::DOUBLE / nullif(sum(i.net_amount), 0) AS gross_margin_pct,
               sum(i.returned_quantity)::DOUBLE / nullif(sum(i.quantity), 0) AS return_rate
        FROM gold.fact_order_items i JOIN gold.fact_orders o USING (order_id)
        WHERE NOT i.is_cancelled AND {where}
        GROUP BY 1 ORDER BY net_revenue DESC
        """,
        params,
    ).df()


def channel_performance(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    """Spend, attributed revenue, ROAS and CAC per channel (region filter not applicable)."""
    where, params = _where(f, "month", None, "channel")
    params[0] = date(f.start.year, f.start.month, 1)  # monthly grain: include the start month
    return con.execute(
        f"""
        SELECT channel, sum(spend)::DOUBLE AS spend,
               sum(attributed_revenue)::DOUBLE AS attributed_revenue,
               sum(attributed_revenue)::DOUBLE / nullif(sum(spend), 0) AS roas,
               sum(spend)::DOUBLE / nullif(sum(new_customers), 0) AS cac,
               sum(new_customers) AS new_customers
        FROM gold.mart_channel_roas WHERE channel_type = 'paid' AND {where}
        GROUP BY 1 ORDER BY roas DESC NULLS LAST
        """,
        params,
    ).df()


def funnel(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    """Sessions reaching each funnel step, by device."""
    where, params = _where(f, "session_date", "region", "traffic_source")
    return con.execute(
        f"""
        SELECT device, count(*) AS sessions,
               count(*) FILTER (WHERE reached_product_view) AS product_view,
               count(*) FILTER (WHERE reached_add_to_cart) AS add_to_cart,
               count(*) FILTER (WHERE reached_checkout) AS checkout,
               count(*) FILTER (WHERE reached_purchase) AS purchase
        FROM gold.fact_sessions WHERE {where}
        GROUP BY 1 ORDER BY sessions DESC
        """,
        params,
    ).df()


def rfm_segments(con: duckdb.DuckDBPyConnection, f: Filters) -> pd.DataFrame:
    """RFM segment sizes and value (region / acquisition-channel filters apply)."""
    where, params = _where(f, "last_order_date", "region", "acquisition_channel")
    # RFM is a snapshot: include every customer whose last order is on/before the end date.
    params[0] = date(1900, 1, 1)
    return con.execute(
        f"""
        SELECT rfm_segment, count(*) AS customers, sum(monetary)::DOUBLE AS revenue,
               avg(monetary)::DOUBLE AS avg_revenue
        FROM gold.mart_rfm WHERE {where}
        GROUP BY 1 ORDER BY revenue DESC
        """,
        params,
    ).df()


def top_pairs(con: duckdb.DuckDBPyConnection, n: int = 10) -> pd.DataFrame:
    """Strongest product pairs by lift (market-basket mart; not filterable)."""
    return con.execute(
        "SELECT product_a_name, product_b_name, orders_with_pair, support, "
        "confidence_a_to_b, lift FROM gold.mart_market_basket "
        "ORDER BY lift DESC, orders_with_pair DESC LIMIT ?",
        [n],
    ).df()
