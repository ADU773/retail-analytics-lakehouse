"""Python models: DAG nodes implemented in Python (like dbt Python models).

Each function receives an open DuckDB connection and a :class:`ModelContext` and returns a
pandas DataFrame, which the runner materialises as ``schema.table``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.transform import clv
from retail_lakehouse.transform.models import Model

log = get_logger(__name__)
PYTHON_MODELS: dict[str, Model] = {}
HORIZON_WEEKS = 52.0
BACKTEST_HOLDOUT_WEEKS = 26
BACKTEST_SCHEMA = {
    "customer_id": "str",
    "calibration_end": "datetime64[us]",
    "x_calibration": "int64",
    "t_x_weeks": "float64",
    "T_weeks": "float64",
    "predicted_holdout_purchases": "float64",
    "naive_holdout_purchases": "float64",
    "actual_holdout_purchases": "int64",
}


@dataclass
class ModelContext:
    """Runtime context passed to Python models."""

    as_of: date
    run_id: str


def python_model(
    name: str, depends_on: list[str], description: str
) -> Callable[[Callable[..., pd.DataFrame]], Callable[..., pd.DataFrame]]:
    """Register a Python model in the DAG."""

    def decorator(fn: Callable[..., pd.DataFrame]) -> Callable[..., pd.DataFrame]:
        PYTHON_MODELS[name] = Model(
            name=name,
            kind="python",
            depends_on=tuple(depends_on),
            materialized="table",
            description=description,
            func=fn,
        )
        return fn

    return decorator


def _purchase_summary(
    con: duckdb.DuckDBPyConnection, cutoff: date, holdout_end: date | None = None
) -> pd.DataFrame:
    """Per-customer RFM-style summary (x, t_x, T in weeks) from non-cancelled orders.

    Only orders up to ``cutoff`` are used; with ``holdout_end`` the number of purchase days in
    ``(cutoff, holdout_end]`` is returned as ``actual_holdout``.
    """
    end = holdout_end or cutoff
    return con.execute(
        f"""
        WITH daily AS (
            SELECT customer_id, order_date,
                   sum(net_revenue) AS revenue, sum(gross_margin) AS margin
            FROM gold.fact_orders
            WHERE NOT is_cancelled
            GROUP BY 1, 2
        ),
        cal AS (
            SELECT customer_id,
                   min(order_date) AS first_order_date,
                   max(order_date) AS last_order_date,
                   count(*) AS purchase_days,
                   sum(revenue) AS revenue,
                   sum(margin) AS margin,
                   arg_min(revenue, order_date) AS first_day_revenue
            FROM daily
            WHERE order_date <= DATE '{cutoff}'
            GROUP BY 1
        ),
        hold AS (
            SELECT customer_id, count(*) AS actual_holdout
            FROM daily
            WHERE order_date > DATE '{cutoff}' AND order_date <= DATE '{end}'
            GROUP BY 1
        )
        SELECT cal.*,
               cal.purchase_days - 1 AS x,
               date_diff('day', first_order_date, last_order_date) / 7.0 AS t_x,
               (date_diff('day', first_order_date, DATE '{cutoff}') + 1) / 7.0 AS "T",
               CASE WHEN purchase_days > 1
                    THEN (revenue - first_day_revenue) / (purchase_days - 1) END AS repeat_aov,
               coalesce(hold.actual_holdout, 0) AS actual_holdout
        FROM cal
        LEFT JOIN hold USING (customer_id)
        ORDER BY customer_id
        """
    ).df()


def _fit(summary: pd.DataFrame) -> tuple[clv.BgNbdParams, clv.GammaGammaParams]:
    x = summary["x"].to_numpy(float)
    t_x = summary["t_x"].to_numpy(float)
    T = summary["T"].to_numpy(float)
    bg = clv.fit_bgnbd(x, t_x, T)
    rep = x > 0
    m = summary["repeat_aov"].fillna(0).to_numpy(float)
    gg = clv.fit_gamma_gamma(x[rep], m[rep])
    return bg, gg


@python_model(
    "gold.mart_customer_ltv",
    depends_on=["gold.fact_orders", "gold.dim_customer"],
    description=(
        "Historical CLV (net revenue and gross margin to date) plus predicted 12-month CLV "
        "from a BG/NBD purchase model and a Gamma-Gamma spend model."
    ),
)
def mart_customer_ltv(con: duckdb.DuckDBPyConnection, ctx: ModelContext) -> pd.DataFrame:
    """One row per purchasing customer with historical and predicted CLV."""
    s = _purchase_summary(con, ctx.as_of)
    if s.empty:
        return s
    x, t_x, T = (s[c].to_numpy(float) for c in ("x", "t_x", "T"))
    bg, gg = _fit(s)
    exp_purchases = clv.bgnbd_expected_purchases(bg, HORIZON_WEEKS, x, t_x, T)
    # Fallback for numerically degenerate rows: shrunk run-rate.
    fallback = (x + bg.r) / (T + bg.alpha) * HORIZON_WEEKS
    exp_purchases = np.where(np.isnan(exp_purchases), fallback, exp_purchases)
    p_alive = clv.bgnbd_probability_alive(bg, x, t_x, T)
    exp_aov = clv.gamma_gamma_expected_value(gg, x, s["repeat_aov"].fillna(0).to_numpy(float))
    overall_margin = float(s["margin"].sum() / s["revenue"].sum())
    margin_rate = np.where(
        s["revenue"] > 0, s["margin"] / s["revenue"].where(s["revenue"] > 0), overall_margin
    )
    margin_rate = np.clip(np.nan_to_num(margin_rate, nan=overall_margin), 0.0, 0.9)

    dims = con.execute(
        "SELECT customer_id, segment, region, acquisition_channel "
        "FROM gold.dim_customer WHERE is_current AND customer_key > 0"
    ).df()
    out = pd.DataFrame(
        {
            "customer_id": s["customer_id"],
            "first_order_date": s["first_order_date"],
            "last_order_date": s["last_order_date"],
            "cohort_month": pd.to_datetime(s["first_order_date"])
            .dt.to_period("M")
            .dt.to_timestamp(),
            "orders": s["purchase_days"].astype("int64"),
            "repeat_purchases": s["x"].astype("int64"),
            "tenure_weeks": T.round(2),
            "recency_weeks": t_x.round(2),
            "historical_revenue": s["revenue"].astype(float).round(2),
            "historical_clv": s["margin"].astype(float).round(2),
            "p_alive": p_alive.round(4),
            "expected_purchases_12m": exp_purchases.round(4),
            "expected_aov": exp_aov.round(2),
            "margin_rate": margin_rate.round(4),
        }
    )
    out["predicted_clv_12m"] = (
        out["expected_purchases_12m"] * out["expected_aov"] * out["margin_rate"]
    ).round(2)
    out["clv_tier"] = pd.qcut(
        out["predicted_clv_12m"].rank(method="first"), 4, labels=["Low", "Medium", "High", "Top"]
    ).astype(str)
    out = out.merge(dims, on="customer_id", how="left")
    params = {
        "bgnbd_r": bg.r,
        "bgnbd_alpha": bg.alpha,
        "bgnbd_a": bg.a,
        "bgnbd_b": bg.b,
        "gg_p": gg.p,
        "gg_q": gg.q,
        "gg_v": gg.v,
        "overall_margin_rate": overall_margin,
        "customers": float(len(out)),
    }
    _store_params(con, "mart_customer_ltv", params)
    log.info(
        "BG/NBD r=%.3f alpha=%.2f a=%.3f b=%.3f | Gamma-Gamma p=%.2f q=%.2f v=%.1f",
        bg.r,
        bg.alpha,
        bg.a,
        bg.b,
        gg.p,
        gg.q,
        gg.v,
    )
    return out


@python_model(
    "gold.mart_clv_backtest",
    depends_on=["gold.fact_orders"],
    description=(
        "Out-of-time validation of the BG/NBD model: fit on all data up to as_of - 26 weeks, "
        "predict purchases in the 26-week holdout and compare with what actually happened."
    ),
)
def mart_clv_backtest(con: duckdb.DuckDBPyConnection, ctx: ModelContext) -> pd.DataFrame:
    """Customer-level predicted vs. actual holdout purchases (plus a naive baseline)."""
    cutoff = ctx.as_of - timedelta(weeks=BACKTEST_HOLDOUT_WEEKS)
    s = _purchase_summary(con, cutoff, holdout_end=ctx.as_of)
    if len(s) < 50:  # too little history before the cutoff to fit a model: empty, typed table
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in BACKTEST_SCHEMA.items()})
    x, t_x, T = (s[c].to_numpy(float) for c in ("x", "t_x", "T"))
    bg, _ = _fit(s)
    pred = clv.bgnbd_expected_purchases(bg, float(BACKTEST_HOLDOUT_WEEKS), x, t_x, T)
    pred = np.where(np.isnan(pred), (x + bg.r) / (T + bg.alpha) * BACKTEST_HOLDOUT_WEEKS, pred)
    naive = x / np.maximum(T, 1.0) * BACKTEST_HOLDOUT_WEEKS  # repeat run-rate baseline
    return pd.DataFrame(
        {
            "customer_id": s["customer_id"],
            "calibration_end": pd.Timestamp(cutoff),
            "x_calibration": s["x"].astype("int64"),
            "t_x_weeks": t_x.round(2),
            "T_weeks": T.round(2),
            "predicted_holdout_purchases": pred.round(4),
            "naive_holdout_purchases": naive.round(4),
            "actual_holdout_purchases": s["actual_holdout"].fillna(0).astype("int64"),
        }
    )


def _store_params(con: duckdb.DuckDBPyConnection, model: str, params: dict[str, Any]) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta.model_params (model VARCHAR, param VARCHAR, value DOUBLE)"
    )
    con.execute("DELETE FROM meta.model_params WHERE model = ?", [model])
    con.executemany(
        "INSERT INTO meta.model_params VALUES (?, ?, ?)",
        [(model, k, float(v)) for k, v in params.items()],
    )
