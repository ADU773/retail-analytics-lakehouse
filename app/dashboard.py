"""Aurora Goods Co. - interactive analytics dashboard (Streamlit).

Reads the gold layer of the DuckDB warehouse produced by ``retail-lakehouse run-all``.

    streamlit run app/dashboard.py
    streamlit run app/dashboard.py -- --warehouse path/to/lakehouse.duckdb

The warehouse path can also be set with the ``RLH_WAREHOUSE`` environment variable.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    from retail_lakehouse.reporting import dashboard_data as dd
    from retail_lakehouse.reporting import theme as t
except ModuleNotFoundError:  # running from a source checkout without `pip install -e .`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from retail_lakehouse.reporting import dashboard_data as dd
    from retail_lakehouse.reporting import theme as t

STEPS = ["sessions", "product_view", "add_to_cart", "checkout", "purchase"]
STEP_LABELS = ["Session", "Product view", "Add to cart", "Checkout", "Purchase"]


def warehouse_path() -> Path:
    """Resolve the warehouse path from ``--warehouse``, $RLH_WAREHOUSE or the default."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--warehouse")
    args, _ = parser.parse_known_args(sys.argv[1:])
    return Path(args.warehouse or os.environ.get(dd.WAREHOUSE_ENV_VAR) or dd.DEFAULT_WAREHOUSE)


def style(fig: go.Figure, height: int = 320) -> go.Figure:
    """Apply the shared report styling to a Plotly figure."""
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 30, "b": 8},
        font={"family": t.FONT, "size": 12},
        legend={"orientation": "h", "y": 1.08, "x": 0},
        bargap=0.35,
    )
    return fig


@st.cache_data(show_spinner=False)
def load(path: str, query: str, filters: dd.Filters | None = None) -> pd.DataFrame | dict:
    """Run one dashboard query on a short-lived read-only connection (cached per filter)."""
    con = dd.connect_readonly(path)
    try:
        fn = getattr(dd, query)
        return fn(con, filters) if filters is not None else fn(con)
    finally:
        con.close()


def main() -> None:
    """Render the dashboard."""
    st.set_page_config(page_title="Aurora Goods Co. analytics", layout="wide")
    st.title("Aurora Goods Co. - commercial dashboard")
    st.caption("Gold layer of the retail-analytics-lakehouse (synthetic data).")

    path = warehouse_path()
    if not path.is_file():
        st.error(f"Warehouse not found at `{path}`. Run `retail-lakehouse run-all` first.")
        return
    opts = load(str(path), "filter_options")

    with st.sidebar:
        st.header("Filters")
        date_range = st.date_input(
            "Order date range",
            value=(opts["min_date"], opts["max_date"]),
            min_value=opts["min_date"],
            max_value=opts["max_date"],
        )
        regions = st.multiselect("Region", opts["regions"], default=[])
        channels = st.multiselect(
            "Channel", opts["channels"], default=[], help="Last-touch channel / traffic source"
        )
        st.caption("Empty selection = all values.")

    if isinstance(date_range, tuple | list) and len(date_range) == 2:
        start, end = date_range
    else:  # the user is midway through picking a range
        start, end = opts["min_date"], opts["max_date"]
    f = dd.Filters(start=start, end=end, regions=tuple(regions), channels=tuple(channels))

    k = load(str(path), "kpis", f)
    cols = st.columns(6)
    tiles = [
        ("Net revenue", t.money(k["net_revenue"])),
        ("Orders", t.num(k["orders"])),
        ("AOV", t.money(k["aov"], 2)),
        ("Gross margin", t.pct(k["gross_margin_pct"])),
        ("Return rate (units)", t.pct(k["return_rate"])),
        ("Customers", t.num(k["customers"])),
    ]
    for col, (label, value) in zip(cols, tiles, strict=True):
        col.metric(label, value)

    if k["orders"] == 0:
        st.info("No orders match the current filters.")
        return

    daily = load(str(path), "daily_revenue", f)
    fig = go.Figure()
    fig.add_scatter(
        x=daily["date"],
        y=daily["net_revenue"],
        name="Daily net revenue",
        line={"color": t.BLUE_RAMP[1], "width": 1},
    )
    fig.add_scatter(
        x=daily["date"],
        y=daily["revenue_7d"],
        name="7-day average",
        line={"color": t.SERIES[0], "width": 2.5},
    )
    fig.update_yaxes(tickprefix="$")
    st.subheader("Revenue trend")
    st.plotly_chart(style(fig, 340))

    left, right = st.columns(2)
    with left:
        st.subheader("Revenue by category")
        cat = load(str(path), "revenue_by_category", f)
        fig = go.Figure(
            go.Bar(
                x=cat["net_revenue"], y=cat["category"], orientation="h", marker_color=t.SERIES[0]
            )
        )
        fig.update_yaxes(autorange="reversed")
        fig.update_xaxes(tickprefix="$")
        st.plotly_chart(style(fig))
    with right:
        st.subheader("Paid channel ROAS (last touch)")
        ch = load(str(path), "channel_performance", f)
        fig = go.Figure(
            go.Bar(
                x=ch["channel"],
                y=ch["roas"],
                marker_color=t.SERIES[0],
                text=[f"{v:.1f}x" for v in ch["roas"].fillna(0)],
                textposition="outside",
            )
        )
        fig.add_hline(y=1.0, line_dash="dot", line_color=t.CRITICAL)
        st.plotly_chart(style(fig))

    left, right = st.columns(2)
    with left:
        st.subheader("Funnel conversion by device")
        fn = load(str(path), "funnel", f)
        fig = go.Figure()
        for i, row in fn.reset_index(drop=True).iterrows():
            rates = [row[s] / row["sessions"] if row["sessions"] else 0 for s in STEPS]
            fig.add_scatter(
                x=STEP_LABELS,
                y=rates,
                name=row["device"],
                mode="lines+markers",
                line={"color": t.DEVICE_COLORS.get(row["device"], t.SERIES[i % 8])},
            )
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(style(fig))
    with right:
        st.subheader("RFM segments")
        rfm = load(str(path), "rfm_segments", f)
        fig = go.Figure(
            go.Bar(
                x=rfm["revenue"],
                y=rfm["rfm_segment"],
                orientation="h",
                marker_color=t.SERIES[0],
                customdata=rfm["customers"],
                hovertemplate="%{y}: $%{x:,.0f} from %{customdata:,} customers<extra></extra>",
            )
        )
        fig.update_yaxes(autorange="reversed")
        fig.update_xaxes(tickprefix="$")
        st.plotly_chart(style(fig))

    st.subheader("Top product pairs by lift")
    st.dataframe(load(str(path), "top_pairs"), hide_index=True)


main()
