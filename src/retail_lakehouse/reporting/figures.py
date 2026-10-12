"""Interactive Plotly figures for the static HTML report (docs/index.html)."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from retail_lakehouse.reporting import theme as t
from retail_lakehouse.reporting.data import ReportData

STEPS = ["sessions", "product_view", "add_to_cart", "checkout", "purchase"]
STEP_LABELS = ["Session", "Product view", "Add to cart", "Checkout", "Purchase"]
PAID = ["paid_search", "paid_social", "display", "affiliate", "email"]


def _style(fig: go.Figure, height: int = 340, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 16, "t": 12, "b": 8},
        paper_bgcolor=t.SURFACE,
        plot_bgcolor=t.SURFACE,
        font={"family": t.FONT, "size": 12, "color": t.INK_2},
        hoverlabel={"bgcolor": "#ffffff", "bordercolor": t.GRID, "font": {"color": t.INK}},
        showlegend=legend,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.0,
            "xanchor": "left",
            "x": 0,
            "font": {"color": t.INK_2},
        },
        bargap=0.35,
    )
    axis = {
        "gridcolor": t.GRID,
        "linecolor": t.AXIS,
        "zeroline": False,
        "tickfont": {"color": t.MUTED},
        "title": {"font": {"color": t.INK_2}},
    }
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    return fig


def revenue_trend(d: ReportData) -> go.Figure:
    """Daily net revenue with a 7-day rolling average and retail-event annotations."""
    df = d.daily.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["rolling"] = df["net_revenue"].rolling(7, min_periods=1).mean()
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["net_revenue"],
            name="Daily net revenue",
            mode="lines",
            line={"color": t.BLUE_RAMP[1], "width": 1},
            hovertemplate="%{x|%a %d %b %Y}<br>Net revenue $%{y:,.0f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["rolling"],
            name="7-day average",
            mode="lines",
            line={"color": t.SERIES[0], "width": 2},
            hovertemplate="%{x|%d %b %Y}<br>7-day avg $%{y:,.0f}<extra></extra>",
        )
    )
    peaks = df[df["holiday_name"].isin(["Black Friday", "Cyber Monday"])]
    top_per_year = peaks.loc[peaks.groupby(peaks["date"].dt.year)["net_revenue"].idxmax()]
    for _, row in top_per_year.iterrows():
        fig.add_annotation(
            x=row["date"],
            y=row["net_revenue"],
            text=f"{row['holiday_name']}<br>${row['net_revenue']:,.0f}",
            showarrow=True,
            arrowhead=0,
            arrowcolor=t.AXIS,
            ax=-60,
            ay=-10,
            font={"size": 11, "color": t.INK_2},
            align="right",
        )
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    return _style(fig, 360)


def monthly_revenue(d: ReportData) -> go.Figure:
    """Monthly net revenue (single series)."""
    m = d.monthly
    fig = go.Figure(
        go.Bar(
            x=pd.to_datetime(m["month_start"]),
            y=m["net_revenue"],
            marker={"color": t.SERIES[0], "cornerradius": 4},
            customdata=m[["orders", "new_customers"]],
            hovertemplate="%{x|%b %Y}<br>Net revenue $%{y:,.0f}<br>Orders %{customdata[0]:,}"
            "<br>New customers %{customdata[1]:,}<extra></extra>",
        )
    )
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    fig.update_xaxes(dtick="M3", tickformat="%b %Y")
    return _style(fig, 300, legend=False)


def weekday_profile(d: ReportData) -> go.Figure:
    """Average net revenue by day of week."""
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    by_day = d.daily.groupby("day_name")["net_revenue"].mean().reindex(order)
    fig = go.Figure(
        go.Bar(
            x=[o[:3] for o in order],
            y=by_day.values,
            marker={"color": t.SERIES[0], "cornerradius": 4},
            text=[f"${v / 1000:,.1f}K" for v in by_day.values],
            textposition="outside",
            textfont={"color": t.INK_2},
            hovertemplate="%{x}: $%{y:,.0f} per day<extra></extra>",
        )
    )
    fig.update_yaxes(tickprefix="$", tickformat=",.0f", range=[0, by_day.max() * 1.18])
    return _style(fig, 300, legend=False)


def cohort_heatmap(d: ReportData, max_months: int = 12) -> go.Figure:
    """Retention heatmap: acquisition cohort x months since first order (single-hue ramp)."""
    c = d.cohorts[d.cohorts["months_since_first_order"] <= max_months]
    c = c[c["is_complete_period"]]
    pivot = c.pivot(index="cohort", columns="months_since_first_order", values="retention_rate")
    sizes = d.cohorts.drop_duplicates("cohort").set_index("cohort")["cohort_size"]
    labels = [f"{idx} (n={sizes.get(idx, 0):,})" for idx in pivot.index]
    scale = [[i / (len(t.BLUE_RAMP) - 1), col] for i, col in enumerate(t.BLUE_RAMP)]
    z = pivot.values
    text = [[("" if pd.isna(v) else f"{v * 100:.0f}%") for v in row] for row in z]
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=[str(c) for c in pivot.columns],
            y=labels,
            colorscale=scale,
            zmin=0,
            zmax=0.3,
            text=text,
            texttemplate="%{text}",
            textfont={"size": 9},
            xgap=2,
            ygap=2,
            colorbar={"title": "Retention", "tickformat": ".0%", "thickness": 10},
            hovertemplate="Cohort %{y}<br>Month %{x}: %{z:.1%} active<extra></extra>",
        )
    )
    fig.update_yaxes(autorange="reversed", gridcolor=t.SURFACE)
    fig.update_xaxes(title_text="Months since first order", gridcolor=t.SURFACE)
    fig = _style(fig, 520, legend=False)
    fig.update_traces(zmin=0, zmax=0.3, selector={"type": "heatmap"})
    # month 0 is 100% by definition; cap the scale so the interesting range is visible
    return fig


def rfm_segments(d: ReportData) -> go.Figure:
    """Share of customers vs share of revenue per RFM segment."""
    r = d.rfm.copy().sort_values("revenue")
    r["cust_share"] = r["customers"] / r["customers"].sum()
    r["rev_share"] = r["revenue"] / r["revenue"].sum()
    fig = go.Figure()
    for col, name, color in (
        ("cust_share", "Share of customers", t.SERIES[0]),
        ("rev_share", "Share of revenue", t.SERIES[1]),
    ):
        fig.add_trace(
            go.Bar(
                y=r["rfm_segment"],
                x=r[col],
                name=name,
                orientation="h",
                marker={"color": color, "cornerradius": 4},
                customdata=r[["customers", "revenue"]],
                hovertemplate="%{y}<br>" + name + " %{x:.1%}<br>%{customdata[0]:,} customers, "
                "$%{customdata[1]:,.0f}<extra></extra>",
            )
        )
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
    fig.update_xaxes(tickformat=".0%")
    return _style(fig, 420)


def channel_roas(d: ReportData) -> go.Figure:
    """ROAS per paid channel with a break-even reference."""
    ch = d.channels[d.channels["channel"].isin(PAID) & (d.channels["spend"] > 0)].sort_values(
        "roas"
    )
    fig = go.Figure(
        go.Bar(
            y=ch["channel"],
            x=ch["roas"],
            orientation="h",
            marker={"color": t.SERIES[0], "cornerradius": 4},
            text=[f"{v:.1f}x" for v in ch["roas"]],
            textposition="outside",
            textfont={"color": t.INK_2},
            customdata=ch[["spend", "revenue", "cac"]],
            hovertemplate="%{y}<br>ROAS %{x:.2f}x<br>Spend $%{customdata[0]:,.0f}"
            "<br>Attributed revenue $%{customdata[1]:,.0f}<extra></extra>",
        )
    )
    fig.add_vline(x=1, line_width=1, line_color=t.MUTED)
    fig.add_annotation(
        x=1,
        y=1.02,
        yref="paper",
        text="break-even",
        showarrow=False,
        font={"size": 10, "color": t.MUTED},
        xanchor="left",
    )
    fig.update_xaxes(type="log", title_text="ROAS (attributed revenue / spend, log scale)")
    return _style(fig, 300, legend=False)


def funnel_by_device(d: ReportData) -> go.Figure:
    """Share of sessions reaching each funnel step, per device."""
    f = d.funnel_device.set_index("device")
    fig = go.Figure()
    for i, device in enumerate(["desktop", "mobile", "tablet"]):
        if device not in f.index:
            continue
        row = f.loc[device]
        rates = [row[s] / row["sessions"] for s in STEPS]
        fig.add_trace(
            go.Bar(
                x=STEP_LABELS,
                y=rates,
                name=device.title(),
                marker={"color": t.SERIES[i], "cornerradius": 4},
                customdata=[row[s] for s in STEPS],
                hovertemplate=device.title()
                + "<br>%{x}: %{y:.1%} of sessions (%{customdata:,.0f})<extra></extra>",
            )
        )
    fig.update_layout(barmode="group", bargroupgap=0.08)
    fig.update_yaxes(tickformat=".0%", title_text="Share of sessions reaching step")
    return _style(fig, 340)


def top_pairs(d: ReportData, n: int = 12) -> go.Figure:
    """Top product pairs by lift."""
    p = d.pairs.head(n).iloc[::-1]
    labels = [f"{a} + {b}" for a, b in zip(p["product_a_name"], p["product_b_name"], strict=True)]
    fig = go.Figure(
        go.Bar(
            y=labels,
            x=p["lift"],
            orientation="h",
            marker={"color": t.SERIES[0], "cornerradius": 4},
            text=[f"{v:.1f}" for v in p["lift"]],
            textposition="outside",
            textfont={"color": t.INK_2},
            customdata=p[["orders_with_pair", "confidence_a_to_b", "support"]],
            hovertemplate="%{y}<br>Lift %{x:.2f}<br>Orders with pair %{customdata[0]:,}"
            "<br>Confidence A->B %{customdata[1]:.1%}<extra></extra>",
        )
    )
    fig.update_xaxes(title_text="Lift (x more often together than by chance)")
    return _style(fig, 420, legend=False)


def clv_by_segment(d: ReportData) -> go.Figure:
    """Average predicted 12-month CLV per RFM segment."""
    s = d.ltv_by_rfm.sort_values("avg_predicted_clv_12m")
    fig = go.Figure(
        go.Bar(
            y=s["rfm_segment"],
            x=s["avg_predicted_clv_12m"],
            orientation="h",
            marker={"color": t.SERIES[0], "cornerradius": 4},
            text=[f"${v:,.0f}" for v in s["avg_predicted_clv_12m"]],
            textposition="outside",
            textfont={"color": t.INK_2},
            customdata=s[["customers", "avg_historical_clv"]],
            hovertemplate="%{y}<br>Predicted 12m CLV $%{x:,.0f}<br>Historical CLV $%{customdata[1]:,.0f}"
            "<br>%{customdata[0]:,} customers<extra></extra>",
        )
    )
    fig.update_xaxes(
        tickprefix="$", title_text="Average predicted 12-month gross margin per customer"
    )
    return _style(fig, 360, legend=False)


def backtest_buckets(bt: pd.DataFrame) -> pd.DataFrame:
    """Mean predicted / naive / actual holdout purchases by calibration repeat count."""
    b = bt.copy()
    b["bucket"] = b["x_calibration"].clip(upper=7)
    g = b.groupby("bucket").agg(
        customers=("customer_id", "count"),
        predicted=("predicted_holdout_purchases", "mean"),
        naive=("naive_holdout_purchases", "mean"),
        actual=("actual_holdout_purchases", "mean"),
    )
    g.index = [f"{i}+" if i == 7 else str(i) for i in g.index]
    return g


def clv_backtest(d: ReportData) -> go.Figure:
    """BG/NBD backtest: predicted vs actual holdout purchases by calibration frequency."""
    if d.backtest.empty:
        return _style(go.Figure(), 300)
    g = backtest_buckets(d.backtest)
    fig = go.Figure()
    for col, name, color in (
        ("actual", "Actual", t.SERIES[0]),
        ("predicted", "BG/NBD", t.SERIES[1]),
        ("naive", "Naive run-rate", t.SERIES[2]),
    ):
        fig.add_trace(
            go.Scatter(
                x=g.index,
                y=g[col],
                name=name,
                mode="lines+markers",
                line={"color": color, "width": 2},
                marker={"size": 8, "line": {"color": t.SURFACE, "width": 2}},
                customdata=g["customers"],
                hovertemplate=name
                + "<br>%{x} repeat purchases in calibration<br>%{y:.2f} purchases "
                "in holdout (n=%{customdata:,})<extra></extra>",
            )
        )
    fig.update_xaxes(title_text="Repeat purchases during calibration period", type="category")
    fig.update_yaxes(title_text="Mean purchases in 26-week holdout")
    return _style(fig, 340)


def quarantine_reasons(d: ReportData) -> go.Figure:
    """Rows quarantined per entity and reason."""
    q = d.quarantine.copy()
    q["label"] = q["entity"] + ": " + q["rejection_reason"]
    q = q.sort_values("rejected_rows")
    fig = go.Figure(
        go.Bar(
            y=q["label"],
            x=q["rejected_rows"],
            orientation="h",
            marker={"color": t.SERIES[0], "cornerradius": 4},
            text=[f"{v:,}" for v in q["rejected_rows"]],
            textposition="outside",
            textfont={"color": t.INK_2},
            customdata=q["share_of_bronze"],
            hovertemplate="%{y}<br>%{x:,} rows (%{customdata:.2%} of bronze)<extra></extra>",
        )
    )
    fig.update_xaxes(title_text="Rows quarantined")
    return _style(fig, max(320, 22 * len(q) + 60), legend=False)


def model_timings(d: ReportData) -> go.Figure:
    """Wall time per model from the latest run log."""
    r = d.run_log.sort_values("seconds").tail(14)
    fig = go.Figure(
        go.Bar(
            y=r["model"],
            x=r["seconds"],
            orientation="h",
            marker={"color": t.SERIES[0], "cornerradius": 4},
            customdata=r["rows"],
            text=[f"{v:.2f}s" for v in r["seconds"]],
            textposition="outside",
            textfont={"color": t.INK_2},
            hovertemplate="%{y}<br>%{x:.2f}s, %{customdata:,} rows<extra></extra>",
        )
    )
    fig.update_xaxes(title_text="Seconds")
    return _style(fig, 380, legend=False)
