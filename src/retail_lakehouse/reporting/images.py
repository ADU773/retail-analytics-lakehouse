"""Static PNG charts (matplotlib) for the README - white background, ~110 dpi."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import FuncFormatter, PercentFormatter

from retail_lakehouse.reporting import theme as t
from retail_lakehouse.reporting.data import ReportData
from retail_lakehouse.reporting.figures import PAID, STEP_LABELS, STEPS, backtest_buckets

DPI = 110
RC = {
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "axes.edgecolor": t.AXIS,
    "axes.labelcolor": t.INK_2,
    "axes.titlecolor": t.INK,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.titlepad": 22,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": t.GRID,
    "grid.linewidth": 0.8,
    "xtick.color": t.MUTED,
    "ytick.color": t.MUTED,
    "xtick.labelcolor": t.INK_2,
    "ytick.labelcolor": t.INK_2,
    "font.size": 10,
    "legend.frameon": False,
    "axes.spines.top": False,
    "axes.spines.right": False,
}
_dollars = FuncFormatter(lambda v, _: f"${v / 1000:,.0f}K" if abs(v) >= 1000 else f"${v:,.0f}")


def _save(fig: plt.Figure, path: Path) -> Path:
    fig.tight_layout()
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def _subtitle(ax: plt.Axes, text: str) -> None:
    ax.text(0, 1.01, text, transform=ax.transAxes, fontsize=9.5, color=t.INK_2, va="bottom")


def revenue_trend(d: ReportData, path: Path) -> Path:
    df = d.daily.copy()
    df["date"] = pd.to_datetime(df["date"])
    roll = df["net_revenue"].rolling(7, min_periods=1).mean()
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(10, 4.2))
        ax.plot(
            df["date"], df["net_revenue"], color=t.BLUE_RAMP[1], lw=0.9, label="Daily net revenue"
        )
        ax.plot(df["date"], roll, color=t.SERIES[0], lw=2, label="7-day average")
        peaks = df[df["holiday_name"].isin(["Black Friday", "Cyber Monday"])]
        if not peaks.empty:
            top = peaks.loc[peaks.groupby(peaks["date"].dt.year)["net_revenue"].idxmax()]
            for _, row in top.iterrows():
                ax.annotate(
                    f"{row['holiday_name']}\n${row['net_revenue'] / 1000:,.1f}K",
                    (row["date"], row["net_revenue"]),
                    xytext=(-70, -6),
                    textcoords="offset points",
                    fontsize=8.5,
                    color=t.INK_2,
                    ha="right",
                    arrowprops={"arrowstyle": "-", "color": t.AXIS, "lw": 0.8},
                )
        ax.yaxis.set_major_formatter(_dollars)
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper left")
        ax.set_title("Net revenue per day")
        _subtitle(ax, "Weekly + annual seasonality with Black Friday / Cyber Monday spikes")
        ax.grid(axis="x", visible=False)
        return _save(fig, path)


def cohort_retention(d: ReportData, path: Path, max_months: int = 12) -> Path:
    c = d.cohorts[
        (d.cohorts["months_since_first_order"] <= max_months) & d.cohorts["is_complete_period"]
    ]
    pivot = c.pivot(index="cohort", columns="months_since_first_order", values="retention_rate")
    cmap = LinearSegmentedColormap.from_list("blue", t.BLUE_RAMP)
    with plt.rc_context({**RC, "axes.grid": False}):
        fig, ax = plt.subplots(figsize=(10, 6.2))
        data = pivot.values.astype(float)
        im = ax.imshow(np.ma.masked_invalid(data), cmap=cmap, vmin=0, vmax=0.3, aspect="auto")
        for (i, j), v in np.ndenumerate(data):
            if not np.isnan(v):
                ax.text(
                    j,
                    i,
                    f"{v * 100:.0f}",
                    ha="center",
                    va="center",
                    fontsize=7.5,
                    color="white" if v > 0.17 else t.INK,
                )
        ax.set_xticks(range(len(pivot.columns)), [str(x) for x in pivot.columns])
        ax.set_yticks(range(len(pivot.index)), list(pivot.index))
        ax.set_xlabel("Months since first order")
        ax.tick_params(length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        cb = fig.colorbar(
            im, ax=ax, fraction=0.03, pad=0.02, format=PercentFormatter(1.0, decimals=0)
        )
        cb.outline.set_visible(False)
        ax.set_title("Cohort retention (% of cohort ordering in month n)")
        _subtitle(
            ax,
            "Monthly acquisition cohorts; month 0 = 100% by definition, colour scale capped at 30%",
        )
        return _save(fig, path)


def rfm_segments(d: ReportData, path: Path) -> Path:
    r = d.rfm.sort_values("revenue")
    y = np.arange(len(r))
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(10, 4.8))
        cs = r["customers"] / r["customers"].sum()
        rs = r["revenue"] / r["revenue"].sum()
        ax.barh(y + 0.2, cs, height=0.38, color=t.SERIES[0], label="Share of customers")
        ax.barh(y - 0.2, rs, height=0.38, color=t.SERIES[1], label="Share of revenue")
        ax.set_yticks(y, r["rfm_segment"])
        ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        ax.grid(axis="y", visible=False)
        ax.legend(loc="lower right")
        ax.set_title("RFM segments: customers vs revenue")
        _subtitle(ax, "Quintile R/F/M scores mapped to named segments")
        return _save(fig, path)


def channel_roas(d: ReportData, path: Path) -> Path:
    ch = d.channels[d.channels["channel"].isin(PAID) & (d.channels["spend"] > 0)].sort_values(
        "roas"
    )
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(10, 3.8))
        ax.barh(ch["channel"], ch["roas"], height=0.5, color=t.SERIES[0])
        for i, (v, s) in enumerate(zip(ch["roas"], ch["spend"], strict=True)):
            ax.text(
                v * 1.04,
                i,
                f"{v:.1f}x  (spend ${s / 1000:,.0f}K)",
                va="center",
                fontsize=9,
                color=t.INK_2,
            )
        ax.axvline(1, color=t.MUTED, lw=1)
        ax.set_xscale("log")
        ax.set_xlim(right=ch["roas"].max() * 4)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
        ax.grid(axis="y", visible=False)
        ax.set_title("Return on ad spend by channel (last-touch)")
        _subtitle(ax, "Attributed net revenue / spend, log scale; grey line = break-even (1x)")
        return _save(fig, path)


def funnel_by_device(d: ReportData, path: Path) -> Path:
    f = d.funnel_device.set_index("device")
    x = np.arange(len(STEPS))
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(10, 4.2))
        devices = [dv for dv in ("desktop", "mobile", "tablet") if dv in f.index]
        w = 0.8 / len(devices)
        for i, dv in enumerate(devices):
            row = f.loc[dv]
            rates = [row[s] / row["sessions"] for s in STEPS]
            ax.bar(
                x + (i - (len(devices) - 1) / 2) * w,
                rates,
                width=w * 0.92,
                color=t.DEVICE_COLORS[dv],
                label=dv.title(),
            )
            ax.text(
                x[-1] + (i - (len(devices) - 1) / 2) * w,
                rates[-1] + 0.015,
                f"{rates[-1] * 100:.0f}%",
                ha="center",
                fontsize=8.5,
                color=t.INK_2,
            )
        ax.set_xticks(x, STEP_LABELS)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        ax.grid(axis="x", visible=False)
        ax.legend()
        ax.set_title("Web funnel by device")
        _subtitle(ax, "Share of sessions reaching each step")
        return _save(fig, path)


def top_pairs(d: ReportData, path: Path, n: int = 10) -> Path:
    p = d.pairs.head(n).iloc[::-1]
    labels = [f"{a}  +  {b}" for a, b in zip(p["product_a_name"], p["product_b_name"], strict=True)]
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(10, 4.6))
        ax.barh(labels, p["lift"], height=0.55, color=t.SERIES[0])
        for i, (v, o) in enumerate(zip(p["lift"], p["orders_with_pair"], strict=True)):
            ax.text(
                v + p["lift"].max() * 0.01,
                i,
                f"{v:.1f}  ({o:,} orders)",
                va="center",
                fontsize=8.5,
                color=t.INK_2,
            )
        ax.set_xlim(right=p["lift"].max() * 1.22)
        ax.tick_params(axis="y", labelsize=8.5)
        ax.grid(axis="y", visible=False)
        ax.set_title("Market basket: top product pairs by lift")
        _subtitle(ax, "Lift = co-purchase rate vs chance; labels show lift (orders)")
        return _save(fig, path)


def clv_backtest(d: ReportData, path: Path) -> Path:
    g = backtest_buckets(d.backtest)
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(10, 4.0))
        for col, name, color in (
            ("actual", "Actual", t.SERIES[0]),
            ("predicted", "BG/NBD forecast", t.SERIES[1]),
            ("naive", "Naive run-rate", t.SERIES[2]),
        ):
            ax.plot(
                g.index,
                g[col],
                color=color,
                lw=2,
                marker="o",
                ms=6,
                mec="white",
                mew=1.5,
                label=name,
            )
        ax.set_xlabel("Repeat purchases during the calibration period")
        ax.set_ylabel("Mean purchases in 26-week holdout")
        ax.grid(axis="x", visible=False)
        ax.legend()
        ax.set_title("CLV model backtest: forecast vs actual")
        _subtitle(ax, "BG/NBD fitted on data up to 26 weeks before the as-of date")
        return _save(fig, path)


def data_quality(d: ReportData, path: Path, n: int = 12) -> Path:
    q = d.quarantine.sort_values("rejected_rows").tail(n)
    labels = q["entity"] + ": " + q["rejection_reason"]
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(10, 4.6))
        ax.barh(labels, q["rejected_rows"], height=0.55, color=t.SERIES[0])
        for i, (v, s) in enumerate(zip(q["rejected_rows"], q["share_of_bronze"], strict=True)):
            ax.text(
                v + q["rejected_rows"].max() * 0.01,
                i,
                f"{v:,}  ({s * 100:.2f}%)",
                va="center",
                fontsize=8.5,
                color=t.INK_2,
            )
        ax.set_xlim(right=q["rejected_rows"].max() * 1.2)
        ax.tick_params(axis="y", labelsize=8.5)
        ax.grid(axis="y", visible=False)
        ax.set_title("Silver-layer quarantine: rejected rows by reason")
        _subtitle(ax, "Share of the entity's bronze rows in brackets; nothing is dropped silently")
        return _save(fig, path)


def benchmark_chart(bench: dict, path: Path) -> Path:
    """Generation scaling and storage footprint from benchmark.json."""
    gen = bench.get("generation", [])
    storage = bench.get("storage_mb", {})
    with plt.rc_context(RC):
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 3.8))
        if gen:
            labels = [f"{g['workers']} worker{'s' if g['workers'] > 1 else ''}" for g in gen]
            vals = [g["rows_per_second"] / 1000 for g in gen]
            a1.bar(labels, vals, width=0.5, color=t.SERIES[0])
            for i, v in enumerate(vals):
                a1.text(i, v * 1.02, f"{v:,.0f}K rows/s", ha="center", fontsize=9, color=t.INK_2)
            a1.set_ylim(0, max(vals) * 1.2)
            a1.set_title("Generation throughput")
            a1.set_ylabel("thousand rows / second")
            a1.grid(axis="x", visible=False)
        if storage:
            names = list(storage)
            vals = [storage[n] for n in names]
            a2.bar(names, vals, width=0.5, color=t.SERIES[0])
            for i, v in enumerate(vals):
                a2.text(i, v * 1.02, f"{v:,.1f} MB", ha="center", fontsize=9, color=t.INK_2)
            a2.set_ylim(0, max(vals) * 1.2)
            a2.set_title("Storage footprint by layer")
            a2.set_ylabel("MB on disk")
            a2.grid(axis="x", visible=False)
        return _save(fig, path)


def build_images(d: ReportData, out_dir: Path) -> list[Path]:
    """Render all README images into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        revenue_trend(d, out_dir / "revenue_trend.png"),
        cohort_retention(d, out_dir / "cohort_retention.png"),
        rfm_segments(d, out_dir / "rfm_segments.png"),
        channel_roas(d, out_dir / "channel_roas.png"),
        funnel_by_device(d, out_dir / "funnel_by_device.png"),
        top_pairs(d, out_dir / "top_product_pairs.png"),
        data_quality(d, out_dir / "data_quality.png"),
    ]
    if not d.backtest.empty:
        paths.append(clv_backtest(d, out_dir / "clv_backtest.png"))
    return paths
