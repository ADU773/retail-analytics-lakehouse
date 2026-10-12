"""Auto-generated, data-driven findings. Every number is computed from the gold layer."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from retail_lakehouse.reporting.data import ReportData
from retail_lakehouse.reporting.theme import money, num, pct

PAID = ("paid_search", "paid_social", "display", "affiliate", "email")


@dataclass
class Insight:
    """One finding: a short headline, an explanatory sentence and the numbers behind it."""

    area: str
    headline: str
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)


def _ok(value: float) -> bool:
    return value is not None and not (isinstance(value, float) and math.isnan(value))


def backtest_metrics(bt: pd.DataFrame) -> dict[str, float]:
    """Aggregate accuracy of the BG/NBD backtest vs. a naive run-rate baseline."""
    if bt.empty:
        return {}
    pred = bt["predicted_holdout_purchases"].astype(float)
    naive = bt["naive_holdout_purchases"].astype(float)
    actual = bt["actual_holdout_purchases"].astype(float)
    return {
        "customers": float(len(bt)),
        "predicted_total": float(pred.sum()),
        "naive_total": float(naive.sum()),
        "actual_total": float(actual.sum()),
        "aggregate_error": float(pred.sum() / actual.sum() - 1) if actual.sum() else float("nan"),
        "naive_aggregate_error": float(naive.sum() / actual.sum() - 1)
        if actual.sum()
        else float("nan"),
        "mae": float((pred - actual).abs().mean()),
        "naive_mae": float((naive - actual).abs().mean()),
        "correlation": float(pred.corr(actual)),
    }


def compute_insights(d: ReportData) -> list[Insight]:
    """Build the list of findings from report data."""
    out: list[Insight] = []
    k = d.kpis
    daily = d.daily.copy()
    daily["date"] = pd.to_datetime(daily["date"])

    # 1. growth
    if _ok(k.get("revenue_yoy", float("nan"))):
        out.append(
            Insight(
                "Growth",
                f"Net revenue grew {pct(k['revenue_yoy'], 0)} year over year",
                f"The last 12 months generated {money(k['revenue_ltm'])} of net revenue vs "
                f"{money(k['revenue_prior'])} in the prior 12 months, on {num(k['orders_ltm'])} orders "
                f"({pct(k['orders_yoy'], 0)} more). Growth is driven by a compounding customer base: "
                f"{pct(k['repeat_rate'], 0)} of purchasing customers have ordered more than once.",
                {"revenue_yoy": k["revenue_yoy"], "revenue_ltm": k["revenue_ltm"]},
            )
        )

    # 2. Black Friday / Cyber Monday
    peaks = daily[daily["holiday_name"].isin(["Black Friday", "Cyber Monday"])]
    if not peaks.empty:
        last_year = int(peaks["date"].dt.year.max())
        yr = daily[daily["date"].dt.year == last_year]
        avg_day = float(yr["net_revenue"].mean())
        top = peaks[peaks["date"].dt.year == last_year].sort_values("net_revenue").iloc[-1]
        bfcm = yr[yr["holiday_name"].isin(["Black Friday", "Black Friday Weekend", "Cyber Monday"])]
        out.append(
            Insight(
                "Seasonality",
                f"{top['holiday_name']} {last_year} was the biggest day: {float(top['net_revenue']) / avg_day:.1f}x an average day",
                f"{top['holiday_name']} brought in {money(top['net_revenue'])} vs a {last_year} daily "
                f"average of {money(avg_day)}; the four-day Black Friday-Cyber Monday window delivered "
                f"{money(bfcm['net_revenue'].sum())} ({pct(bfcm['net_revenue'].sum() / yr['net_revenue'].sum())} "
                f"of the year's revenue in 1.1% of its days).",
                {"peak_multiple": float(top["net_revenue"]) / avg_day},
            )
        )

    # 3. weekly pattern
    by_day = daily.groupby("day_name")["net_revenue"].mean().sort_values()
    if len(by_day) == 7:
        out.append(
            Insight(
                "Seasonality",
                f"{by_day.index[-1]} is the strongest weekday, {pct(by_day.iloc[-1] / by_day.iloc[0] - 1, 0)} above {by_day.index[0]}",
                f"Average daily net revenue ranges from {money(by_day.iloc[0])} on {by_day.index[0]}s "
                f"to {money(by_day.iloc[-1])} on {by_day.index[-1]}s - "
                + (
                    "schedule e-mail sends and promotions for the weekend peak."
                    if by_day.index[-1] in ("Saturday", "Sunday")
                    else f"time e-mail sends and promotions to land before {by_day.index[-1]}."
                ),
                {"weekday_spread": float(by_day.iloc[-1] / by_day.iloc[0] - 1)},
            )
        )

    # 4. retention
    c = d.cohorts[d.cohorts["is_complete_period"]]
    m1 = c[c["months_since_first_order"] == 1]["retention_rate"].mean()
    m6 = c[c["months_since_first_order"] == 6]["retention_rate"].mean()
    m12 = c[c["months_since_first_order"] == 12]["retention_rate"].mean()
    if _ok(m1):
        tail = f" and {pct(m12)} in month 12" if _ok(m12) else ""
        out.append(
            Insight(
                "Retention",
                f"{pct(m1)} of new customers buy again in their second month",
                f"Averaged over complete cohorts, {pct(m1)} of a cohort is active in month 1, "
                f"{pct(m6) if _ok(m6) else 'n/a'} in month 6{tail}. The steepest drop happens right "
                "after the first order, so the post-purchase journey is the main retention lever.",
                {"m1": m1, "m6": m6, "m12": m12},
            )
        )

    # 5. RFM concentration
    rfm = d.rfm.set_index("rfm_segment")
    total_c, total_r = rfm["customers"].sum(), rfm["revenue"].sum()
    if "Champions" in rfm.index:
        top_two = rfm.loc[[s for s in ("Champions", "Loyal Customers") if s in rfm.index]]
        risk = rfm.loc[[s for s in ("At Risk", "Cannot Lose Them") if s in rfm.index]]
        out.append(
            Insight(
                "Customers",
                f"Champions and Loyal Customers are {pct(top_two['customers'].sum() / total_c, 0)} of buyers but {pct(top_two['revenue'].sum() / total_r, 0)} of revenue",
                f"{num(risk['customers'].sum())} customers sit in At Risk / Cannot Lose Them with "
                f"{money(risk['revenue'].sum())} of historical revenue - the highest-value win-back audience.",
                {
                    "top_share_customers": top_two["customers"].sum() / total_c,
                    "top_share_revenue": top_two["revenue"].sum() / total_r,
                },
            )
        )

    # 6. segments
    seg = d.segments.set_index("customer_segment")
    if {"plus", "standard"} <= set(seg.index):
        ratio = seg.loc["plus", "orders_per_customer"] / seg.loc["standard", "orders_per_customer"]
        detail = (
            f"Plus members average {seg.loc['plus', 'orders_per_customer']:.1f} orders each vs "
            f"{seg.loc['standard', 'orders_per_customer']:.1f} for Standard customers"
        )
        if "business" in seg.index:
            detail += (
                f"; Business accounts order less often but at a {money(seg.loc['business', 'aov'])} "
                f"AOV ({seg.loc['business', 'aov'] / seg.loc['standard', 'aov']:.1f}x Standard)"
            )
        out.append(
            Insight(
                "Customers",
                f"Plus membership members purchase {ratio:.1f}x as often",
                detail + ".",
                {"plus_frequency_ratio": ratio},
            )
        )

    # 7. channels
    ch = d.channels[d.channels["channel"].isin(PAID) & (d.channels["spend"] > 0)].copy()
    if not ch.empty:
        best = ch.sort_values("roas").iloc[-1]
        worst = ch.sort_values("roas").iloc[0]
        acq = ch[ch["channel"] != "email"].sort_values("cac")
        out.append(
            Insight(
                "Marketing",
                f"{best['channel'].replace('_', ' ').capitalize()} returns {best['roas']:.1f}x its spend; {worst['channel']} only {worst['roas']:.2f}x",
                f"On last-touch attribution, {worst['channel']} spent {money(worst['spend'])} for "
                f"{money(worst['revenue'])} of revenue (POAS {worst['poas']:.2f} - it loses money after "
                f"product costs). Customer acquisition cost ranges from {money(acq.iloc[0]['cac'])} "
                f"({acq.iloc[0]['channel']}) to {money(acq.iloc[-1]['cac'])} ({acq.iloc[-1]['channel']}). "
                "Last-touch under-credits upper-funnel channels, so treat display as a test-and-learn budget.",
                {"best_roas": float(best["roas"]), "worst_roas": float(worst["roas"])},
            )
        )

    # 8. funnel by device
    f = d.funnel_device.set_index("device")
    if {"desktop", "mobile"} <= set(f.index):
        cr = f["purchase"] / f["sessions"]
        share = f.loc["mobile", "sessions"] / f["sessions"].sum()
        cart_drop = 1 - f.loc["mobile", "add_to_cart"] / f.loc["mobile", "product_view"]
        out.append(
            Insight(
                "Funnel",
                f"Mobile drives {pct(share, 0)} of sessions but converts at {pct(cr['mobile'])} vs {pct(cr['desktop'])} on desktop",
                f"The largest mobile leak is product view -> add to cart ({pct(cart_drop, 0)} of mobile "
                "product viewers never add to cart); a faster mobile product page is the highest-leverage "
                "CRO test. Note: anonymous traffic is down-sampled in this synthetic dataset, so absolute "
                "conversion rates are higher than typical e-commerce - compare devices relative to each other.",
                {"mobile_share": share, "mobile_cr": cr["mobile"], "desktop_cr": cr["desktop"]},
            )
        )

    # 9. market basket
    if not d.pairs.empty:
        top = d.pairs.iloc[0]
        out.append(
            Insight(
                "Merchandising",
                f"'{top['product_a_name']}' + '{top['product_b_name']}' is the strongest bundle (lift {top['lift']:.1f})",
                f"The pair appears in {num(top['orders_with_pair'])} orders; {pct(top['confidence_a_to_b'], 0)} "
                f"of baskets with the first item also contain the second. {num(len(d.pairs))} product pairs "
                "clear the minimum-support threshold - candidates for 'frequently bought together' widgets.",
                {"top_lift": float(top["lift"])},
            )
        )

    # 10. returns
    r = d.returns_by_category
    if not r.empty:
        top = r.iloc[0]
        out.append(
            Insight(
                "Operations",
                f"{top['category']} has the highest return rate at {pct(top['return_rate'])}",
                f"The most common reason is '{top['top_reason']}' (overall return rate "
                f"{pct(k['return_rate'])} of units); a size guide on {top['category']} PDPs is the obvious fix.",
                {"top_return_rate": float(top["return_rate"])},
            )
        )

    # 11. CLV
    bt = backtest_metrics(d.backtest)
    if bt:
        out.append(
            Insight(
                "CLV",
                f"The BG/NBD model forecast 26-week holdout purchases within {pct(abs(bt['aggregate_error']))} in aggregate",
                f"Fitted on data up to 26 weeks before the as-of date, it predicted {num(bt['predicted_total'])} "
                f"purchases for {num(bt['customers'])} customers vs {num(bt['actual_total'])} actual; "
                f"customer-level MAE {bt['mae']:.2f} vs {bt['naive_mae']:.2f} for a naive repeat-rate "
                f"baseline. Average predicted 12-month CLV is {money(k['avg_predicted_clv_12m'], 2)} of gross margin.",
                bt,
            )
        )

    # 12. data quality
    e = d.extra
    if e.get("bronze_rows_with_rejects"):
        q = (
            d.quarantine.groupby("rejection_reason")["rejected_rows"]
            .sum()
            .sort_values(ascending=False)
        )
        out.append(
            Insight(
                "Data quality",
                f"{num(e['quarantined_rows'])} raw rows ({pct(e['quarantined_rows'] / e['bronze_rows_with_rejects'], 2)}) were quarantined, none silently dropped",
                f"Top reasons: {', '.join(f'{name} ({num(v)})' for name, v in q.head(3).items())}. "
                f"{num(e['nonstandard_ts'])} order timestamps in non-standard formats/time zones were "
                f"normalised to UTC, {num(e['late_orders'])} orders and {num(e['late_events'])} events "
                f"arrived in a later batch, and {num(e['scd2_customers'])} customers have SCD2 history.",
                {"quarantine_share": e["quarantined_rows"] / e["bronze_rows_with_rejects"]},
            )
        )
    return out


def insights_markdown(insights: list[Insight], d: ReportData) -> str:
    """Render ``docs/insights.md``."""
    lines = [
        "# Aurora Goods Co. - data-driven insights",
        "",
        f"_Auto-generated by `retail-lakehouse report` from the gold layer (as of {d.as_of.isoformat()}). "
        "Every number below is computed from the pipeline run that produced this file; "
        "the data is synthetic (see the README)._",
        "",
    ]
    for i, ins in enumerate(insights, 1):
        lines += [f"## {i}. {ins.headline}", "", f"**{ins.area}.** {ins.detail}", ""]
    lines += [
        "## Method notes",
        "",
        "- Revenue = net revenue (list price x quantity - discounts), excluding shipping and "
        "cancelled orders.",
        "- Attribution = last touch: the traffic source of the session containing the purchase event.",
        "- RFM scores are quintiles as of the as-of date; segments use the classic R x F map.",
        "- CLV = BG/NBD expected purchases (52 weeks) x Gamma-Gamma expected order value x the "
        "customer's historical gross-margin rate. The backtest refits the model on data up to "
        "26 weeks before the as-of date.",
        "",
    ]
    return "\n".join(lines)
