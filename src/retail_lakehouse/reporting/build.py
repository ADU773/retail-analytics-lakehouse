"""``retail-lakehouse report``: build docs/index.html, docs/images/*.png and companion docs."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb
import jinja2
from markupsafe import Markup

from retail_lakehouse.config import Settings
from retail_lakehouse.logging_setup import get_logger
from retail_lakehouse.quality.runner import LAYERS, load_layer
from retail_lakehouse.reporting import figures
from retail_lakehouse.reporting.data import ReportData, load_report_data
from retail_lakehouse.reporting.insights import (
    Insight,
    backtest_metrics,
    compute_insights,
    insights_markdown,
)
from retail_lakehouse.reporting.theme import money, num, pct
from retail_lakehouse.warehouse import connect

log = get_logger(__name__)
REPO_URL = "https://github.com/ADU773/retail-analytics-lakehouse"
PLOTLY_CONFIG = {"displayModeBar": False, "responsive": True}


def _fig_html(fig: Any, first: bool) -> Markup:
    html = fig.to_html(
        full_html=False,
        include_plotlyjs="cdn" if first else False,
        config=PLOTLY_CONFIG,
        default_width="100%",
    )
    return Markup(html)


def _kpi_tiles(d: ReportData) -> list[dict[str, str]]:
    k = d.kpis

    def delta(key: str) -> tuple[str, str]:
        v = k.get(key)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return "", ""
        arrow = "+" if v >= 0 else "-"
        return f"{arrow}{abs(v) * 100:.0f}% YoY", "up" if v >= 0 else "down"

    rev_delta, rev_cls = delta("revenue_yoy")
    ord_delta, ord_cls = delta("orders_yoy")
    return [
        {
            "label": "Net revenue",
            "value": money(k["net_revenue"]),
            "delta": rev_delta,
            "delta_class": rev_cls,
            "note": f"LTM {money(k['revenue_ltm'])}",
        },
        {
            "label": "Orders",
            "value": num(k["orders"]),
            "delta": ord_delta,
            "delta_class": ord_cls,
            "note": f"{num(k['units'])} units",
        },
        {
            "label": "Average order value",
            "value": money(k["aov"], 2),
            "note": "net revenue / orders",
        },
        {"label": "Gross margin", "value": pct(k["gross_margin_pct"]), "note": "after COGS"},
        {"label": "Return rate", "value": pct(k["return_rate"]), "note": "returned / sold units"},
        {
            "label": "Session conversion",
            "value": pct(k["conversion_rate"]),
            "note": f"{num(k['sessions'])} sessions",
        },
        {
            "label": "Purchasing customers",
            "value": num(k["customers"]),
            "note": f"{pct(k['repeat_rate'], 0)} repeat buyers",
        },
        {
            "label": "Marketing efficiency",
            "value": f"{k['mer']:.1f}x",
            "note": f"revenue / {money(k['marketing_spend'])} spend",
        },
        {
            "label": "Predicted 12m CLV",
            "value": money(k["avg_predicted_clv_12m"], 2),
            "note": "avg gross margin / customer",
        },
    ]


def _channel_rows(d: ReportData) -> list[dict[str, str]]:
    rows = []
    for _, r in d.channels.iterrows():
        paid = r["spend"] > 0

        def fmt(v: Any, f: Any, show: bool = True) -> str:
            return f(v) if show and v == v and v is not None else "-"

        rows.append(
            {
                "channel": r["channel"],
                "type": "paid" if paid else "organic / owned",
                "spend": money(r["spend"]) if paid else "-",
                "orders": num(r["orders"]),
                "revenue": money(r["revenue"]),
                "roas": fmt(r["roas"], lambda v: f"{v:.2f}x", paid),
                "poas": fmt(r["poas"], lambda v: f"{v:.2f}x", paid),
                "new_customers": num(r["new_customers"]),
                "cac": fmt(r["cac"], lambda v: money(v, 2), paid and r["channel"] != "email"),
            }
        )
    return rows


def _dq_rows(d: ReportData) -> list[dict[str, Any]]:
    rows = []
    for layer in LAYERS:
        sub = d.dq_summary[d.dq_summary["layer"] == layer]
        if sub.empty:
            continue
        c = dict(zip(sub["status"], sub["tests"], strict=True))
        rows.append(
            {
                "layer": layer,
                "total": int(sum(c.values())),
                "pass": int(c.get("pass", 0)),
                "warn": int(c.get("warn", 0)),
                "fail": int(c.get("fail", 0) + c.get("error", 0)),
            }
        )
    return rows


def _load_json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def build_html(
    settings: Settings, d: ReportData, insights: list[Insight], run_summary: dict | None
) -> str:
    """Render the dashboard HTML."""
    fig_builders = {
        "revenue_trend": figures.revenue_trend,
        "monthly": figures.monthly_revenue,
        "weekday": figures.weekday_profile,
        "cohort": figures.cohort_heatmap,
        "rfm": figures.rfm_segments,
        "clv_segment": figures.clv_by_segment,
        "roas": figures.channel_roas,
        "funnel": figures.funnel_by_device,
        "pairs": figures.top_pairs,
        "backtest": figures.clv_backtest,
        "quarantine": figures.quarantine_reasons,
        "timings": figures.model_timings,
    }
    fig_html = {name: _fig_html(fn(d), i == 0) for i, (name, fn) in enumerate(fig_builders.items())}
    bt = backtest_metrics(d.backtest)
    p = d.clv_params
    clv_text = (
        f"A BG/NBD model (r={p.get('bgnbd_r', 0):.2f}, alpha={p.get('bgnbd_alpha', 0):.1f}, "
        f"a={p.get('bgnbd_a', 0):.2f}, b={p.get('bgnbd_b', 0):.2f}) and a Gamma-Gamma spend model are fitted "
        "by maximum likelihood (scipy). Refitting on data up to 26 weeks before the as-of date, the forecast of "
        f"holdout purchases was {num(bt.get('predicted_total', 0))} vs {num(bt.get('actual_total', 0))} actual "
        f"({bt.get('aggregate_error', 0) * 100:+.1f}%); customer-level MAE {bt.get('mae', 0):.2f} vs "
        f"{bt.get('naive_mae', 0):.2f} for a naive repeat-rate baseline."
        if bt
        else "Not enough history for a backtest at this scale."
    )
    e = d.extra
    dq_text = (
        f"{num(e.get('quarantined_rows', 0))} of {num(e.get('bronze_rows_with_rejects', 0))} raw rows "
        f"({pct(e.get('quarantined_rows', 0) / max(e.get('bronze_rows_with_rejects', 1), 1), 2)}) were "
        "quarantined by the silver layer with an explicit rejection reason; every other injected problem "
        "(mixed timestamp formats and time zones, padded/mixed-case codes, invalid e-mails, late-arriving "
        "batches, stale CDC versions) was repaired."
    )
    pipeline_rows, storage_rows, hardware = [], [], "the build machine"
    if run_summary:
        pipeline_rows = [
            {
                "step": s["step"],
                "seconds": f"{s['seconds']:.1f} s",
                "rss": f"{s['peak_rss_mb']:,.0f} MB",
            }
            for s in run_summary.get("steps", [])
        ]
        st = run_summary.get("storage_bytes", {})
        storage_rows = [{"layer": k, "size": f"{v / 1e6:,.1f} MB"} for k, v in st.items()]
        sysinfo = run_summary.get("system", {})
        hardware = (
            f"{sysinfo.get('cpu_model', 'CPU')} ({sysinfo.get('physical_cores', '?')} cores / "
            f"{sysinfo.get('logical_cores', '?')} threads, {sysinfo.get('total_ram_gb', '?')} GB RAM)"
        )
    returns = [
        {
            "category": r["category"],
            "units": num(r["units"]),
            "returned": num(r["returned_units"]),
            "rate": pct(r["return_rate"]),
            "reason": r["top_reason"],
        }
        for _, r in d.returns_by_category.iterrows()
    ]
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(resources.files("retail_lakehouse").joinpath("templates"))
        ),
        autoescape=jinja2.select_autoescape(["html", "j2"]),
    )
    first_day = str(d.daily["date"].min())[:10] if not d.daily.empty else ""
    return env.get_template("report.html.j2").render(
        period=f"{first_day} to {d.as_of.isoformat()}",
        as_of=d.as_of.isoformat(),
        generated=datetime.now(UTC).strftime("%Y-%m-%d"),
        repo_url=REPO_URL,
        kpis=_kpi_tiles(d),
        fig=fig_html,
        channel_rows=_channel_rows(d),
        return_rows=returns,
        insights=insights,
        clv_text=clv_text,
        dq_text=dq_text,
        dq_rows=_dq_rows(d),
        pipeline_rows=pipeline_rows,
        storage_rows=storage_rows,
        threads=settings.resources.effective_threads(),
        hardware=hardware,
    )


def data_dictionary(settings: Settings, con: duckdb.DuckDBPyConnection) -> str:
    """Markdown data dictionary: model descriptions + column types + YAML column docs."""
    from retail_lakehouse.transform.runner import build_dag

    dag = build_dag(settings)
    docs: dict[str, Any] = {}
    for layer in LAYERS:
        docs.update(load_layer(settings, layer).tables)
    cols = con.execute(
        "SELECT table_schema || '.' || table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema IN ('silver', 'gold') ORDER BY table_schema DESC, table_name, ordinal_position"
    ).fetchall()
    counts = {
        f"{s}.{t}": n
        for s, t, n in con.execute(
            "SELECT schema_name, table_name, estimated_size FROM duckdb_tables() "
            "WHERE schema_name IN ('silver', 'gold')"
        ).fetchall()
    }
    by_table: dict[str, list[tuple[str, str]]] = {}
    for table, col, typ in cols:
        by_table.setdefault(table, []).append((col, typ))
    lines = [
        "# Data dictionary",
        "",
        "_Generated by `retail-lakehouse report` from the DuckDB catalog, the model headers "
        "(`-- description:`) and the column docs in the data-quality YAML._",
        "",
        "Lineage columns present on silver tables: `_batch_id` (landing batch), `_source_file` "
        "(raw file path relative to the data dir), `_ingested_at` (bronze load time, UTC).",
        "",
    ]
    for layer in ("gold", "silver"):
        lines += [f"## {layer.title()} layer", ""]
        for table in sorted(t for t in by_table if t.startswith(layer + ".")):
            model = dag.models.get(table)
            desc = (model.description if model else "") or (
                docs[table].description if table in docs else ""
            )
            kind = f"{model.kind} model" if model else "table"
            lines += [
                f"### `{table}`",
                "",
                f"{desc}",
                "",
                f"_{kind}; ~{counts.get(table, 0):,} rows in the latest run._",
                "",
                "| Column | Type | Description |",
                "|---|---|---|",
            ]
            col_docs = docs[table].columns if table in docs else {}
            for col, typ in by_table[table]:
                lines.append(f"| `{col}` | {typ} | {col_docs.get(col, '')} |")
            lines.append("")
    return "\n".join(lines)


def dq_markdown(con: duckdb.DuckDBPyConnection, as_of: str) -> str:
    """Latest result per test across all layers, as Markdown."""
    rows = con.execute(
        """
        SELECT layer, test_id, severity, status, failures, total, message
        FROM meta.dq_results
        QUALIFY row_number() OVER (PARTITION BY layer, test_id ORDER BY executed_at DESC) = 1
        ORDER BY CASE layer WHEN 'bronze' THEN 1 WHEN 'silver' THEN 2 ELSE 3 END, test_id
        """
    ).fetchall()
    summary: dict[str, dict[str, int]] = {}
    for layer, _, _, status, *_ in rows:
        summary.setdefault(layer, {"pass": 0, "warn": 0, "fail": 0, "error": 0})[status] += 1
    lines = [
        "# Data-quality results",
        "",
        f"_Latest result of every test (as-of date {as_of}), generated by `retail-lakehouse report`. "
        "Specs live in `src/retail_lakehouse/dq/<layer>.yaml`._",
        "",
        "| Layer | Tests | Pass | Warn | Fail / error |",
        "|---|---:|---:|---:|---:|",
    ]
    for layer, c in summary.items():
        lines.append(
            f"| {layer} | {sum(c.values())} | {c['pass']} | {c['warn']} | {c['fail'] + c['error']} |"
        )
    lines += [
        "",
        "Bronze warnings are expected: they measure the problems injected into the landing "
        "zone, which silver repairs or quarantines.",
        "",
        "| Layer | Test | Severity | Status | Failing rows | Rows checked | Detail |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for layer, test_id, severity, status, failures, total, message in rows:
        msg = (message or "").replace("|", "\\|")
        tid = test_id.replace("|", "\\|")
        lines.append(
            f"| {layer} | `{tid}` | {severity} | {status} | {failures:,} | {total:,} | {msg} |"
        )
    return "\n".join(lines) + "\n"


def update_readme_block(readme: Path, marker: str, content: str) -> bool:
    """Replace the text between ``<!-- MARKER:START -->`` and ``<!-- MARKER:END -->``."""
    if not readme.exists():
        return False
    text = readme.read_text(encoding="utf-8")
    pattern = re.compile(rf"(<!-- {marker}:START -->)(.*?)(<!-- {marker}:END -->)", re.DOTALL)
    if not pattern.search(text):
        return False
    new = pattern.sub(lambda m: f"{m.group(1)}\n{content.strip()}\n{m.group(3)}", text)
    readme.write_text(new, encoding="utf-8")
    return True


def _retention_text(insights: list[Insight]) -> str:
    ins = next((i for i in insights if i.area == "Retention"), None)
    if ins is None:
        return "-"
    parts = [
        f"month {m}: {pct(ins.metrics[k])}"
        for m, k in ((1, "m1"), (6, "m6"), (12, "m12"))
        if ins.metrics.get(k) is not None and not math.isnan(ins.metrics[k])
    ]
    return ", ".join(parts) or "-"


def key_results_markdown(d: ReportData, insights: list[Insight], run_summary: dict | None) -> str:
    """Markdown table for the README 'Key results' block."""
    k, e = d.kpis, d.extra
    bt = backtest_metrics(d.backtest)
    rows = [
        (
            "Net revenue (2 years, synthetic)",
            f"{money(k['net_revenue'])} from {num(k['orders'])} orders "
            f"(YoY {k.get('revenue_yoy', 0) * 100:+.0f}%)",
        ),
        (
            "Gross margin / AOV / return rate",
            f"{pct(k['gross_margin_pct'])} / {money(k['aov'], 2)} / {pct(k['return_rate'])}",
        ),
        ("Cohort retention (share of cohort ordering)", _retention_text(insights)),
        (
            "Best vs worst paid channel",
            next((i.headline for i in insights if i.area == "Marketing"), "-"),
        ),
        ("Device conversion gap", next((i.headline for i in insights if i.area == "Funnel"), "-")),
        (
            "CLV backtest (26-week holdout)",
            f"{bt.get('aggregate_error', 0) * 100:+.1f}% aggregate error, MAE {bt.get('mae', 0):.2f} vs "
            f"{bt.get('naive_mae', 0):.2f} naive"
            if bt
            else "-",
        ),
        (
            "Rows quarantined by silver",
            f"{num(e.get('quarantined_rows', 0))} "
            f"({pct(e.get('quarantined_rows', 0) / max(e.get('bronze_rows_with_rejects', 1), 1), 2)} of raw rows)",
        ),
    ]
    if run_summary:
        dq = run_summary.get("dq", {})
        tests = sum(sum(v.values()) for v in dq.values())
        fails = sum(v.get("fail", 0) + v.get("error", 0) for v in dq.values())
        warns = sum(v.get("warn", 0) for v in dq.values())
        rows.append(
            (
                "Data-quality tests",
                f"{tests} tests: {tests - fails - warns} pass, {warns} warn (bronze), {fails} fail",
            )
        )
        steps = run_summary.get("steps", [])
        single = max((s["peak_rss_mb"] for s in steps if s["step"] != "generate"), default=0.0)
        rows.append(
            (
                "End-to-end run-all (default scale)",
                f"{run_summary['total_seconds']:.0f} s; peak RSS {single:,.0f} MB for the "
                f"single-process steps, {run_summary['peak_rss_mb']:,.0f} MB summed over all "
                f"{run_summary['settings']['workers'] + 1} processes while generating",
            )
        )
    out = ["| Metric | Result |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows]
    return "\n".join(out)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float):
        return None if math.isnan(obj) else round(obj, 6)
    if hasattr(obj, "item"):
        return _jsonable(obj.item())
    return obj


def build_report(settings: Settings, update_readme: bool = False) -> dict[str, Path]:
    """Build the static report and companion docs from the gold layer."""
    docs = Path(settings.paths.docs_dir)
    images_dir = docs / "images"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / ".nojekyll").touch()
    run_summary = _load_json(settings.paths.reports_dir / "run_summary.json")
    con = connect(settings, read_only=True)
    try:
        d = load_report_data(con, settings.as_of)
        insights = compute_insights(d)
        html = build_html(settings, d, insights, run_summary)
        dictionary = data_dictionary(settings, con)
        dq_md = dq_markdown(con, settings.as_of.isoformat())
    finally:
        con.close()

    out: dict[str, Path] = {}
    out["html"] = docs / "index.html"
    out["html"].write_text(html, encoding="utf-8")
    out["insights"] = docs / "insights.md"
    out["insights"].write_text(insights_markdown(insights, d), encoding="utf-8")
    out["dictionary"] = docs / "data_dictionary.md"
    out["dictionary"].write_text(dictionary, encoding="utf-8")
    out["data_quality"] = docs / "data_quality.md"
    out["data_quality"].write_text(dq_md, encoding="utf-8")
    if settings.report.images:
        from retail_lakehouse.reporting.images import benchmark_chart, build_images

        build_images(d, images_dir)
        bench = _load_json(docs / "benchmark.json")
        if bench:
            benchmark_chart(bench, images_dir / "benchmark.png")

    metrics = {
        "as_of": d.as_of.isoformat(),
        "kpis": d.kpis,
        "data_quality": d.extra,
        "clv_model": {"params": d.clv_params, "backtest": backtest_metrics(d.backtest)},
        "insights": [asdict(i) for i in insights],
        "pipeline": {
            k: run_summary[k]
            for k in (
                "system",
                "settings",
                "steps",
                "total_seconds",
                "peak_rss_mb",
                "generation",
                "dq",
                "storage_bytes",
            )
            if run_summary and k in run_summary
        },
    }
    out["metrics"] = docs / "metrics.json"
    out["metrics"].write_text(json.dumps(_jsonable(metrics), indent=2), encoding="utf-8")
    if update_readme:
        ok = update_readme_block(
            Path(settings.paths.readme),
            "KEY_RESULTS",
            key_results_markdown(d, insights, run_summary),
        )
        log.info("README key-results block %s", "updated" if ok else "not found (markers missing)")
        bench_json = _load_json(docs / "benchmark.json")
        if bench_json:
            from retail_lakehouse.benchmark import benchmark_markdown

            update_readme_block(
                Path(settings.paths.readme), "BENCHMARK", benchmark_markdown(bench_json)
            )
    log.info(
        "report written to %s (%d insights, %.0f KB html)",
        docs,
        len(insights),
        out["html"].stat().st_size / 1024,
    )
    return out
