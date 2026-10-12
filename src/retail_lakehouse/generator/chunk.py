"""Per-month chunk generation (runs inside worker processes).

A chunk turns the global order skeleton for one calendar month into fully detailed raw
records - customers (new signups + CDC change records), orders, order lines, returns, web
sessions and funnel events - injects data-quality problems and lands them as files in the
month's batch folder. Each chunk uses its own deterministic seed, so the output is identical
no matter how many worker processes are used.
"""

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from retail_lakehouse.generator import catalog
from retail_lakehouse.generator import quality as dq
from retail_lakehouse.generator.behavior import OrderSkeleton
from retail_lakehouse.generator.calendar import DailyProfile
from retail_lakehouse.generator.catalog import ProductTable
from retail_lakehouse.generator.customers import (
    SECONDS_PER_DAY,
    CustomerTable,
    sample_seconds_of_day,
)
from retail_lakehouse.generator.writer import write_entity

Columns = dict[str, np.ndarray]

ACQ_TO_SOURCE = np.array([catalog.TRAFFIC_SOURCES.index(c) for c in catalog.ACQUISITION_CHANNELS])
LOC_REGION = np.array([loc[0] for loc in catalog.LOCATIONS], dtype=object)
LOC_COUNTRY = np.array([loc[1] for loc in catalog.LOCATIONS], dtype=object)
LOC_CITY = np.array([loc[2] for loc in catalog.LOCATIONS], dtype=object)
LOC_UTC_OFFSET = np.array([catalog.COUNTRY_UTC_OFFSET[c] for c in LOC_COUNTRY])
SOURCES = np.array(catalog.TRAFFIC_SOURCES, dtype=object)
DEVICES = np.array(catalog.DEVICES, dtype=object)
SEGMENTS = np.array(catalog.SEGMENTS, dtype=object)
ACQ_CHANNELS = np.array(catalog.ACQUISITION_CHANNELS, dtype=object)
BROWSE_TO_PURCHASE_RATIO = 1.6
LINGER_SESSIONS_PER_MONTH = 0.06


@dataclass
class GeneratorParams:
    """Scalar knobs shipped to every worker."""

    seed: int
    n_customers: int
    dq_issue_rate: float
    late_arrival_rate: float
    anonymous_sessions_per_customer: float


@dataclass
class SharedState:
    """Read-only data shared by all chunks (sent once per worker process)."""

    params: GeneratorParams
    profile: DailyProfile
    products: ProductTable
    customers: CustomerTable
    skeleton: OrderSkeleton
    raw_dir: str


@dataclass
class ChunkTask:
    """One calendar month of the generated period."""

    index: int
    month_start: date
    day_lo: int
    day_hi: int  # exclusive

    @property
    def batch_id(self) -> str:
        return batch_for(self.month_start, 0)


@dataclass
class ChunkResult:
    """Summary returned by a worker."""

    index: int
    rows: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    revenue_by_day_source: np.ndarray | None = None
    seconds: float = 0.0


def batch_for(month_start: date, offset: int) -> str:
    """Batch id (``YYYY-MM``) of the month ``offset`` months after ``month_start``."""
    months = month_start.year * 12 + (month_start.month - 1) + offset
    return f"{months // 12:04d}-{months % 12 + 1:02d}"


_SHARED: SharedState | None = None


def init_worker(shared: SharedState | str) -> None:
    """Pool initializer: keep the shared state in a module global.

    ``shared`` may be the path of a pickle written by the parent. Loading it here keeps the
    payload that ``spawn`` pushes through each child's pipe tiny: with the state passed
    inline, the parent blocks until every child has started and read ~8 MB, so workers
    would start one after another instead of in parallel.
    """
    global _SHARED
    if isinstance(shared, str):
        with open(shared, "rb") as fh:
            shared = pickle.load(fh)  # written by this run's parent process
    _SHARED = shared


def _rate(params: GeneratorParams, kind: str) -> float:
    return params.dq_issue_rate * dq.ISSUE_SHARE[kind]


class _Sink:
    """Collects files written by a chunk."""

    def __init__(self, raw_dir: str, result: ChunkResult) -> None:
        from pathlib import Path

        self.raw_dir = Path(raw_dir)
        self.result = result

    def write(self, entity: str, batch: str, name: str, cols: Columns, fmt: str) -> None:
        rel, rows = write_entity(self.raw_dir, entity, batch, name, cols, fmt)
        if rows:
            self.result.files.append(rel)
            self.result.rows[entity] = self.result.rows.get(entity, 0) + rows


def run_chunk(task: ChunkTask, shared: SharedState | None = None) -> ChunkResult:
    """Generate and land all raw records for one month."""
    t0 = time.perf_counter()
    state = shared or _SHARED
    if state is None:  # pragma: no cover - programming error
        raise RuntimeError("worker not initialised with shared state")
    result = ChunkResult(index=task.index)
    rng = np.random.default_rng([state.params.seed, 7919, task.index])
    sink = _Sink(state.raw_dir, result)

    _customers_batch(rng, state, task, sink)
    orders = _orders_and_lines(rng, state, task, sink)
    _sessions_and_events(rng, state, task, sink, orders)
    result.revenue_by_day_source = orders["revenue_by_day_source"]
    result.seconds = time.perf_counter() - t0
    return result


# --------------------------------------------------------------------------------------
# customers
# --------------------------------------------------------------------------------------
def _customers_batch(
    rng: np.random.Generator, state: SharedState, task: ChunkTask, sink: _Sink
) -> None:
    c = state.customers
    p = state.params
    start = np.datetime64(state.profile.start, "s")
    lo = start + np.timedelta64(task.day_lo * SECONDS_PER_DAY, "s")
    hi = start + np.timedelta64(task.day_hi * SECONDS_PER_DAY, "s")

    idx_parts, ts_parts = [], []
    for arr in (c.signup_ts, c.tier_change_ts, c.move_ts, c.email_change_ts):
        mask = ~np.isnat(arr) & (arr >= lo) & (arr < hi)
        idx_parts.append(np.flatnonzero(mask))
        ts_parts.append(arr[mask])
    idx = np.concatenate(idx_parts)
    ts = np.concatenate(ts_parts)
    if not len(idx):
        return
    order = np.argsort(ts, kind="stable")
    idx, ts = idx[order], ts[order]
    st = c.state_at(idx, ts)
    loc = st["location_idx"]
    cols: Columns = {
        "customer_id": c.customer_id[idx],
        "first_name": c.first_name[idx],
        "last_name": c.last_name[idx],
        "email": dq.invalid_emails(rng, st["email"], _rate(p, "emails")),
        "signup_ts": dq.messy_timestamps(
            rng, c.signup_ts[idx], _rate(p, "timestamps"), LOC_UTC_OFFSET[c.location_idx[idx]]
        ),
        "acquisition_channel": dq.messy_categorical(
            rng, ACQ_CHANNELS[c.acquisition_idx[idx]], _rate(p, "categorical")
        ),
        "region": dq.messy_categorical(rng, LOC_REGION[loc], _rate(p, "categorical")),
        "country": LOC_COUNTRY[loc],
        "city": LOC_CITY[loc],
        "segment": dq.messy_categorical(rng, SEGMENTS[st["segment_idx"]], _rate(p, "categorical")),
        "marketing_opt_in": c.marketing_opt_in[idx],
        "updated_at": dq.iso_utc(ts),
    }
    cols = dq.duplicate_rows(rng, cols, _rate(p, "duplicates"))
    sink.write("customers", task.batch_id, f"customers_{task.batch_id}", cols, "jsonl")


# --------------------------------------------------------------------------------------
# orders, order lines, returns
# --------------------------------------------------------------------------------------
def _orders_and_lines(
    rng: np.random.Generator, state: SharedState, task: ChunkTask, sink: _Sink
) -> dict[str, np.ndarray]:
    prof, prods, c, k, p = (
        state.profile,
        state.products,
        state.customers,
        state.skeleton,
        state.params,
    )
    n_src = len(catalog.TRAFFIC_SOURCES)
    revenue = np.zeros((task.day_hi - task.day_lo, n_src))
    osel = np.flatnonzero((k.day_idx >= task.day_lo) & (k.day_idx < task.day_hi))
    n_o = len(osel)
    empty = {"revenue_by_day_source": revenue, "n": np.array(0)}
    if n_o == 0:
        return empty

    start = np.datetime64(prof.start, "s")
    end_ts = np.datetime64(prof.end, "s") + np.timedelta64(SECONDS_PER_DAY - 1, "s")
    month_lo = start + np.timedelta64(task.day_lo * SECONDS_PER_DAY, "s")
    cust = k.cust_idx[osel]
    ots = k.order_ts[osel]
    is_first = k.seq[osel] == 1
    st = c.state_at(cust, ots)
    seg = st["segment_idx"]

    # ---- promotions & discounts
    promos = prof.promotions
    p_idx = prof.promo_index[k.day_idx[osel]]
    usage = np.array([pr.usage_prob for pr in promos] + [0.0])[p_idx]
    pct_tbl = np.array([pr.discount_pct for pr in promos] + [0.0])
    code_tbl = np.array([pr.code for pr in promos] + [None], dtype=object)
    use_promo = (p_idx >= 0) & (rng.random(n_o) < usage)
    welcome = ~use_promo & is_first & (rng.random(n_o) < 0.35)
    pct = np.where(use_promo, pct_tbl[p_idx], np.where(welcome, 0.10, 0.0))
    code = np.where(use_promo, code_tbl[p_idx], np.where(welcome, "WELCOME10", None)).astype(object)

    # ---- basket composition with "frequently bought together" affinities
    available = prods.launched_at <= month_lo
    weights = catalog.category_month_weights(prods, task.month_start.month) * available
    weights = weights / weights.sum()
    n_prod = len(prods)
    n_lines = 1 + np.minimum(rng.poisson(catalog.SEGMENT_BASKET_EXTRA[seg]), 5)
    first = rng.choice(n_prod, size=n_o, p=weights)
    ord_parts, prod_parts = [np.arange(n_o)], [first]
    for j in range(1, 6):
        m = np.flatnonzero(n_lines > j)
        if not len(m):
            break
        partner = prods.partner[first[m]]
        ok = (partner >= 0) & available[np.maximum(partner, 0)]
        u = rng.random(len(m))
        use_partner = ok & (u < 0.22)  # the designated "bought together" product
        use_comp = ~use_partner & (u < 0.40)  # any product from a complementary subcategory
        comp = _complement_choice(rng, prods, first[m], weights)
        rand = rng.choice(n_prod, size=len(m), p=weights)
        ord_parts.append(m)
        prod_parts.append(
            np.where(use_partner, partner, np.where(use_comp & (comp >= 0), comp, rand))
        )
    key = np.concatenate(ord_parts) * n_prod + np.concatenate(prod_parts)
    uniq, counts = np.unique(key, return_counts=True)
    li_order, li_prod = uniq // n_prod, uniq % n_prod
    n_l = len(uniq)
    group_start = np.r_[True, li_order[1:] != li_order[:-1]]
    line_no = np.arange(n_l) - np.maximum.accumulate(np.where(group_start, np.arange(n_l), 0)) + 1

    seg_l = seg[li_order]
    base_qty = np.where(
        seg_l == 2,
        1 + rng.poisson(1.5, n_l),
        rng.choice([1, 2, 3], size=n_l, p=[0.82, 0.14, 0.04]),
    )
    qty = base_qty + counts - 1
    unit_price = prods.list_price[li_prod]
    disc = np.round(qty * unit_price * pct[li_order], 2)
    net = qty * unit_price - disc
    subtotal = np.bincount(li_order, weights=net, minlength=n_o)
    shipping = np.where((subtotal >= 75) | (seg == 1), 0.0, 6.95)

    # ---- status lifecycle
    age_days = (end_ts - ots).astype(float) / SECONDS_PER_DAY
    cancelled = rng.random(n_o) < 0.025
    status = np.where(
        cancelled,
        "cancelled",
        np.where(age_days >= 7, "delivered", np.where(age_days >= 2, "shipped", "placed")),
    ).astype(object)
    upd_days = np.where(
        cancelled,
        rng.uniform(0.05, 1.0, n_o),
        np.where(
            status == "delivered",
            rng.uniform(3, 7, n_o),
            np.where(status == "shipped", rng.uniform(1, 2, n_o), 0.0),
        ),
    )
    updated = np.minimum(
        ots + (upd_days * SECONDS_PER_DAY).astype(np.int64).astype("timedelta64[s]"), end_ts
    )

    # ---- attribution source of the purchase session (needed for spend + sessions)
    source = np.where(
        is_first,
        ACQ_TO_SOURCE[c.acquisition_idx[cust]],
        rng.choice(len(catalog.TRAFFIC_SOURCES), size=n_o, p=catalog.REPEAT_SOURCE_WEIGHTS),
    )
    keep = ~cancelled
    np.add.at(revenue, (k.day_idx[osel][keep] - task.day_lo, source[keep]), subtotal[keep])

    order_ids = k.order_id[osel]
    utc_off = LOC_UTC_OFFSET[st["location_idx"]]
    orders: Columns = {
        "order_id": order_ids,
        "customer_id": dq.orphan_keys(rng, c.customer_id[cust], _rate(p, "orphans"), "C", 7),
        "order_ts": dq.messy_timestamps(rng, ots, _rate(p, "timestamps"), utc_off),
        "status": dq.messy_categorical(rng, status, _rate(p, "categorical")),
        "payment_method": np.array(catalog.PAYMENT_METHODS, dtype=object)[
            rng.choice(len(catalog.PAYMENT_METHODS), size=n_o, p=catalog.PAYMENT_WEIGHTS)
        ],
        "discount_code": code,
        "shipping_fee": shipping,
        "order_total": np.round(subtotal + shipping, 2),
        "currency": np.full(n_o, "USD", dtype=object),
        "updated_at": dq.iso_utc(updated),
    }
    # Stale CDC versions: an older "shipped" snapshot re-sent for some delivered orders.
    stale = dq.pick(rng, n_o, _rate(p, "duplicates"))
    stale = stale[status[stale] == "delivered"]
    if len(stale):
        old = dq.take(orders, stale)
        old["status"] = np.full(len(stale), "shipped", dtype=object)
        old["updated_at"] = dq.iso_utc(ots[stale] + np.timedelta64(SECONDS_PER_DAY, "s"))
        orders = dq.concat([orders, old])

    created = ots[li_order]
    items: Columns = {
        "order_item_id": np.array(
            [f"{o}-{n:02d}" for o, n in zip(order_ids[li_order], line_no, strict=True)],
            dtype=object,
        ),
        "order_id": dq.orphan_keys(rng, order_ids[li_order], _rate(p, "orphans") / 2, "ORD", 8),
        "product_id": dq.orphan_keys(rng, prods.product_id[li_prod], _rate(p, "orphans"), "P", 4),
        "quantity": dq.bad_quantities(rng, qty.astype(np.int64), _rate(p, "quantities")),
        "unit_price": unit_price,
        "discount_amount": disc,
        "created_at": dq.messy_timestamps(rng, created, _rate(p, "timestamps")),
    }

    # ---- late-arriving orders (with their lines) land 1-2 batches later
    late = rng.random(n_o) < p.late_arrival_rate
    late_offset = np.where(rng.random(n_o) < 0.75, 1, 2)
    n_main = n_o  # rows beyond n_o in `orders` are stale versions -> main batch
    late_rows = np.r_[late, np.zeros(len(orders["order_id"]) - n_main, dtype=bool)]
    late_lines = late[li_order]
    main_orders = dq.duplicate_rows(rng, dq.take(orders, ~late_rows), _rate(p, "duplicates"))
    main_items = dq.duplicate_rows(rng, dq.take(items, ~late_lines), _rate(p, "duplicates"))
    sink.write("orders", task.batch_id, f"orders_{task.batch_id}", main_orders, "csv")
    sink.write("order_items", task.batch_id, f"order_items_{task.batch_id}", main_items, "csv")
    for off in (1, 2):
        sel = late & (late_offset == off)
        if sel.any():
            batch = batch_for(task.month_start, off)
            name = f"{batch}_late_from_{task.batch_id}"
            sel_rows = np.r_[sel, np.zeros(len(orders["order_id"]) - n_main, dtype=bool)]
            sink.write("orders", batch, f"orders_{name}", dq.take(orders, sel_rows), "csv")
            sink.write(
                "order_items", batch, f"order_items_{name}", dq.take(items, sel[li_order]), "csv"
            )

    _returns(
        rng,
        state,
        task,
        sink,
        li_order,
        li_prod,
        qty,
        unit_price,
        pct,
        ots,
        cancelled,
        seg_l,
        order_ids,
        items["order_item_id"],
        end_ts,
    )

    return {
        "revenue_by_day_source": revenue,
        "n": np.array(n_o),
        "ots": ots,
        "cust": cust,
        "source": source,
        "li_order": li_order,
        "li_prod": li_prod,
        "order_ids": order_ids,
        "location_idx": st["location_idx"],
    }


def _complement_choice(
    rng: np.random.Generator, prods: ProductTable, anchor: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """For each anchor product, draw a product from a complementary subcategory (-1 if none)."""
    out = np.full(len(anchor), -1, dtype=np.int64)
    for sub in np.unique(prods.subcategory[anchor]):
        comps = catalog.COMPLEMENTS.get(str(sub), [])
        cand = np.flatnonzero(np.isin(prods.subcategory, comps) & (weights > 0))
        if not len(cand):
            continue
        sel = np.flatnonzero(prods.subcategory[anchor] == sub)
        w = weights[cand] / weights[cand].sum()
        out[sel] = rng.choice(cand, size=len(sel), p=w)
    return out


def _returns(
    rng: np.random.Generator,
    state: SharedState,
    task: ChunkTask,
    sink: _Sink,
    li_order: np.ndarray,
    li_prod: np.ndarray,
    qty: np.ndarray,
    unit_price: np.ndarray,
    pct: np.ndarray,
    ots: np.ndarray,
    cancelled: np.ndarray,
    seg_l: np.ndarray,
    order_ids: np.ndarray,
    item_ids: np.ndarray,
    end_ts: np.datetime64,
) -> None:
    prods, p = state.products, state.params
    prob = prods.return_rate[li_prod] * catalog.SEGMENT_RETURN_MULT[seg_l]
    prob = prob * np.where(pct[li_order] >= 0.3, 1.3, 1.0)
    ret = (rng.random(len(li_order)) < prob) & ~cancelled[li_order]
    ret_ts = ots[li_order] + (rng.uniform(4, 35, len(li_order)) * SECONDS_PER_DAY).astype(
        np.int64
    ).astype("timedelta64[s]")
    ret &= ret_ts <= end_ts
    r = np.flatnonzero(ret)
    if not len(r):
        return
    apparel = prods.category[li_prod[r]] == "Apparel"
    reasons_a, w_a = catalog.RETURN_REASONS_APPAREL
    reasons_o, w_o = catalog.RETURN_REASONS_OTHER
    reason = np.where(
        apparel,
        np.array(reasons_a, dtype=object)[rng.choice(len(reasons_a), size=len(r), p=w_a)],
        np.array(reasons_o, dtype=object)[rng.choice(len(reasons_o), size=len(r), p=w_o)],
    ).astype(object)
    q = qty[r]
    q_ret = np.where(q > 1, rng.integers(1, np.maximum(q, 1) + 1), 1)
    refund = np.round(q_ret * unit_price[r] * (1 - pct[li_order[r]]), 2)
    prefix = task.batch_id.replace("-", "")
    cols: Columns = {
        "return_id": np.array([f"RET{prefix}{i + 1:05d}" for i in range(len(r))], dtype=object),
        "order_item_id": dq.orphan_keys(rng, item_ids[r], _rate(p, "orphans"), "ORD", 11),
        "order_id": order_ids[li_order[r]],
        "product_id": prods.product_id[li_prod[r]],
        "quantity": q_ret.astype(np.int64),
        "refund_amount": refund,
        "reason": dq.messy_categorical(rng, reason, _rate(p, "categorical")),
        "requested_at": dq.iso_utc(ret_ts[r]),
        "status": np.full(len(r), "refunded", dtype=object),
    }
    months = ret_ts[r].astype("datetime64[M]")
    for month in np.unique(months):
        sel = months == month
        batch = str(month)
        part = dq.duplicate_rows(rng, dq.take(cols, sel), _rate(p, "duplicates"))
        name = (
            f"returns_{batch}"
            if batch == task.batch_id
            else f"returns_{batch}_from_{task.batch_id}"
        )
        sink.write("returns", batch, name, part, "jsonl")


# --------------------------------------------------------------------------------------
# web sessions and funnel events
# --------------------------------------------------------------------------------------
def _sessions_and_events(
    rng: np.random.Generator,
    state: SharedState,
    task: ChunkTask,
    sink: _Sink,
    orders: dict[str, np.ndarray],
) -> None:
    prof, prods, c, k, p = (
        state.profile,
        state.products,
        state.customers,
        state.skeleton,
        state.params,
    )
    start = np.datetime64(prof.start, "s")
    n_prod = len(prods)
    month_lo = start + np.timedelta64(task.day_lo * SECONDS_PER_DAY, "s")
    weights = catalog.category_month_weights(prods, task.month_start.month) * (
        prods.launched_at <= month_lo
    )
    weights = weights / weights.sum()

    sess: list[Columns] = []
    ev: list[Columns] = []
    n_o = int(orders["n"])

    # ---- 1) purchase sessions: one per order, full funnel ending in a purchase event
    if n_o:
        ots, cust = orders["ots"], orders["cust"]
        li_order, li_prod = orders["li_order"], orders["li_prod"]
        lines_per_order = np.bincount(li_order, minlength=n_o)
        extra_view = rng.random(n_o) < 0.35
        k_purchase = 2 * lines_per_order + 2 + extra_view  # offset of the purchase event
        gap = rng.uniform(30, 100, n_o)
        group_start = np.r_[True, li_order[1:] != li_order[:-1]]
        pos = np.arange(len(li_order)) - np.maximum.accumulate(
            np.where(group_start, np.arange(len(li_order)), 0)
        )
        ev_sess = np.concatenate(
            [li_order, li_order, np.flatnonzero(extra_view), np.arange(n_o), np.arange(n_o)]
        )
        ev_type = np.concatenate(
            [
                np.full(len(li_order), "product_view", dtype=object),
                np.full(len(li_order), "add_to_cart", dtype=object),
                np.full(int(extra_view.sum()), "product_view", dtype=object),
                np.full(n_o, "checkout", dtype=object),
                np.full(n_o, "purchase", dtype=object),
            ]
        )
        offset = np.concatenate(
            [
                extra_view[li_order] + 2 * pos,
                extra_view[li_order] + 2 * pos + 1,
                np.zeros(int(extra_view.sum())),
                k_purchase - 1,
                k_purchase,
            ]
        )
        ev_prod = np.concatenate(
            [
                prods.product_id[li_prod],
                prods.product_id[li_prod],
                prods.product_id[rng.choice(n_prod, size=int(extra_view.sum()), p=weights)],
                np.full(2 * n_o, None, dtype=object),
            ]
        )
        ev_order = np.concatenate(
            [np.full(len(ev_sess) - n_o, None, dtype=object), orders["order_ids"]]
        )
        back = ((k_purchase[ev_sess] - offset) * gap[ev_sess]).astype(np.int64)
        ev_ts = ots[ev_sess] - back.astype("timedelta64[s]")
        s_start = ots - ((k_purchase * gap) + rng.uniform(5, 60, n_o)).astype(np.int64).astype(
            "timedelta64[s]"
        )
        sess.append(
            {
                "visitor_id": c.visitor_id[cust],
                "customer_id": c.customer_id[cust],
                "session_start": s_start,
                "device": rng.choice(3, size=n_o, p=catalog.PURCHASE_DEVICE_WEIGHTS),
                "traffic_source": orders["source"],
                "country": LOC_COUNTRY[orders["location_idx"]],
            }
        )
        ev.append(
            {
                "sess": ev_sess,
                "event_type": ev_type,
                "event_ts": ev_ts,
                "product_id": ev_prod,
                "order_id": ev_order,
            }
        )

    # ---- 2) browse sessions of registered customers (active + lingering churned)
    lo, hi = float(task.day_lo), float(task.day_hi)
    a = np.maximum(c.signup_t, lo)
    b = np.minimum(hi, k.inactive_t)
    active_days = np.clip(b - a, 0.0, None)
    mean_traffic = float(prof.demand[task.day_lo : task.day_hi].mean())
    n_active = rng.poisson(c.rate / 365.0 * BROWSE_TO_PURCHASE_RATIO * active_days * mean_traffic)
    lingering = (k.inactive_t < lo) & (c.signup_t < lo)
    n_linger = np.where(
        lingering, rng.poisson(LINGER_SESSIONS_PER_MONTH * (hi - lo) / 30.0, len(c)), 0
    )
    b_cust = np.concatenate(
        [np.repeat(np.arange(len(c)), n_active), np.repeat(np.arange(len(c)), n_linger)]
    )
    t_active = np.repeat(a, n_active) + rng.random(int(n_active.sum())) * np.repeat(b - a, n_active)
    t_linger = lo + rng.random(int(n_linger.sum())) * (hi - lo)
    b_t = np.concatenate([t_active, t_linger])
    # anonymous traffic
    total_anon = p.anonymous_sessions_per_customer * p.n_customers
    w_days = prof.traffic[task.day_lo : task.day_hi] / prof.traffic.sum()
    anon_counts = rng.poisson(total_anon * w_days)
    a_day = np.repeat(np.arange(task.day_lo, task.day_hi), anon_counts)
    n_b, n_a = len(b_cust), len(a_day)
    m = n_b + n_a
    if m:
        day = np.concatenate([np.floor(b_t).astype(np.int64), a_day])
        secs = day * SECONDS_PER_DAY + sample_seconds_of_day(rng, m)
        floor_secs = np.concatenate(
            [
                (np.maximum(c.signup_t[b_cust], lo) * SECONDS_PER_DAY).astype(np.int64),
                np.full(n_a, int(lo * SECONDS_PER_DAY)),
            ]
        )
        secs = np.clip(secs, floor_secs + 1, int(hi * SECONDS_PER_DAY) - 3600)
        s_start = start + secs.astype("timedelta64[s]")
        anon_loc = rng.choice(len(catalog.LOCATIONS), size=n_a, p=catalog.LOCATION_WEIGHTS)
        b_loc = c.state_at(b_cust, s_start[:n_b])["location_idx"] if n_b else np.empty(0, int)
        anon_vid = np.array(
            [f"A{v:010x}" for v in rng.integers(0, 1 << 40, size=n_a)], dtype=object
        )
        device = rng.choice(3, size=m, p=catalog.BROWSE_DEVICE_WEIGHTS)
        source = rng.choice(len(catalog.TRAFFIC_SOURCES), size=m, p=catalog.BROWSE_SOURCE_WEIGHTS)
        sess.append(
            {
                "visitor_id": np.concatenate([c.visitor_id[b_cust], anon_vid]),
                "customer_id": np.concatenate(
                    [c.customer_id[b_cust], np.full(n_a, None, dtype=object)]
                ),
                "session_start": s_start,
                "device": device,
                "traffic_source": source,
                "country": np.concatenate([LOC_COUNTRY[b_loc], LOC_COUNTRY[anon_loc]]),
            }
        )
        # funnel progression for non-purchase sessions
        probs = np.array([catalog.FUNNEL_PROBS[d] for d in catalog.DEVICES])[device]
        engage = np.array([catalog.SOURCE_ENGAGEMENT[s] for s in catalog.TRAFFIC_SOURCES])[source]
        viewed = rng.random(m) < probs[:, 0]
        n_views = np.where(viewed, 1 + rng.poisson(0.6, m), 0)
        carted = viewed & (rng.random(m) < np.minimum(0.95, probs[:, 1] * engage))
        checked = carted & (rng.random(m) < probs[:, 2])
        v_sess = np.repeat(np.arange(m), n_views)
        v_pos = np.arange(len(v_sess)) - np.repeat(np.cumsum(n_views) - n_views, n_views)
        c_sess = np.flatnonzero(carted)
        k_sess = np.flatnonzero(checked)
        offset_base = n_o  # session index offset (purchase sessions come first)
        gap = rng.uniform(20, 120, m)
        e_sess = np.concatenate([v_sess, c_sess, k_sess])
        e_off = np.concatenate([v_pos + 1, n_views[c_sess] + 1, n_views[k_sess] + 2])
        e_ts = s_start[e_sess] + (e_off * gap[e_sess]).astype(np.int64).astype("timedelta64[s]")
        ev.append(
            {
                "sess": e_sess + offset_base,
                "event_type": np.concatenate(
                    [
                        np.full(len(v_sess), "product_view", dtype=object),
                        np.full(len(c_sess), "add_to_cart", dtype=object),
                        np.full(len(k_sess), "checkout", dtype=object),
                    ]
                ),
                "event_ts": e_ts,
                "product_id": np.concatenate(
                    [
                        prods.product_id[
                            rng.choice(n_prod, size=len(v_sess) + len(c_sess), p=weights)
                        ],
                        np.full(len(k_sess), None, dtype=object),
                    ]
                ),
                "order_id": np.full(len(e_sess), None, dtype=object),
            }
        )

    if not sess:
        return
    s = dq.concat(sess)
    n_s = len(s["session_start"])
    s_order = np.argsort(s["session_start"], kind="stable")
    rank = np.empty(n_s, dtype=np.int64)
    rank[s_order] = np.arange(n_s)
    prefix = task.batch_id.replace("-", "")
    sid = np.array([f"S{prefix}{i + 1:06d}" for i in range(n_s)], dtype=object)
    sessions: Columns = {
        "session_id": sid,
        "visitor_id": s["visitor_id"][s_order],
        "customer_id": dq.orphan_keys(
            rng, s["customer_id"][s_order], _rate(p, "orphans") / 3, "C", 7
        ),
        "session_start": dq.messy_timestamps(
            rng, s["session_start"][s_order], _rate(p, "timestamps")
        ),
        "device": dq.messy_categorical(rng, DEVICES[s["device"][s_order]], _rate(p, "categorical")),
        "traffic_source": dq.messy_categorical(
            rng, SOURCES[s["traffic_source"][s_order]], _rate(p, "categorical")
        ),
        "country": s["country"][s_order],
    }
    # anonymous rows must stay anonymous after orphan injection
    anon_rows = s["customer_id"][s_order] == None  # noqa: E711 - elementwise on object array
    sessions["customer_id"][anon_rows] = None
    sessions = dq.duplicate_rows(rng, sessions, _rate(p, "duplicates"))
    sink.write("web_sessions", task.batch_id, f"web_sessions_{task.batch_id}", sessions, "csv")

    e = dq.concat(ev)
    if not e:
        return
    e_order = np.lexsort((e["event_type"].astype(str), e["event_ts"]))
    n_e = len(e_order)
    events: Columns = {
        "event_id": np.array([f"E{prefix}{i + 1:07d}" for i in range(n_e)], dtype=object),
        "session_id": dq.orphan_keys(
            rng, sid[rank[e["sess"][e_order]]], _rate(p, "orphans"), "S", 12
        ),
        "event_type": dq.messy_categorical(
            rng, e["event_type"][e_order], _rate(p, "categorical") / 2
        ),
        "event_ts": dq.messy_timestamps(rng, e["event_ts"][e_order], _rate(p, "timestamps")),
        "product_id": e["product_id"][e_order],
        "order_id": e["order_id"][e_order],
    }
    late = rng.random(n_e) < p.late_arrival_rate
    main = dq.duplicate_rows(rng, dq.take(events, ~late), _rate(p, "duplicates"))
    sink.write("web_events", task.batch_id, f"web_events_{task.batch_id}", main, "csv")
    if late.any():
        batch = batch_for(task.month_start, 1)
        sink.write(
            "web_events",
            batch,
            f"web_events_{batch}_late_from_{task.batch_id}",
            dq.take(events, late),
            "csv",
        )
