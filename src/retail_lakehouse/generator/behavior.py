"""Order skeleton: when does each customer purchase?

The purchase process is a seasonally modulated Poisson process per customer (simulated by
thinning) combined with a per-purchase dropout probability - the same "buy till you die"
story that BG/NBD models assume, with real-world misspecification added on top (seasonality,
tier changes, first-order conversion). It is computed once, globally, so that the expensive
per-order details can be generated in parallel by date range.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from retail_lakehouse.generator.calendar import DailyProfile
from retail_lakehouse.generator.customers import (
    SECONDS_PER_DAY,
    CustomerTable,
    sample_seconds_of_day,
)

MAX_ORDERS_PER_CUSTOMER = 80


@dataclass
class OrderSkeleton:
    """Timing of every order, sorted by time. ``inactive_t`` is per customer."""

    order_id: np.ndarray
    cust_idx: np.ndarray
    order_ts: np.ndarray  # datetime64[s]
    day_idx: np.ndarray
    seq: np.ndarray  # 1 = first order of the customer
    inactive_t: np.ndarray  # per customer: day offset from which they stop buying

    def __len__(self) -> int:
        return len(self.order_id)


def simulate_orders(
    rng: np.random.Generator, customers: CustomerTable, profile: DailyProfile
) -> OrderSkeleton:
    """Simulate order times for all customers over the profile period."""
    n = len(customers)
    horizon = float(profile.n_days)
    demand = profile.demand
    max_demand = profile.max_demand

    # First order: often in the signup session, otherwise days later.
    same_session = rng.random(n) < 0.55
    delay = np.where(
        same_session,
        rng.uniform(5 / 1440, 90 / 1440, size=n),
        rng.exponential(6.0, size=n) + 0.05,
    )
    first_t = customers.signup_t + delay
    has_first = customers.converts & (first_t < horizon)

    cust_parts = [np.flatnonzero(has_first)]
    time_parts = [first_t[has_first]]
    counts = has_first.astype(np.int64)

    p_drop = customers.dropout_p
    change_t = customers.tier_change_t(np.datetime64(profile.start, "s"))
    mult = customers.tier_rate_mult
    lam_max = customers.rate / 365.0 * np.maximum(1.0, mult) * max_demand

    t = first_t.copy()
    inactive_t = np.where(has_first, np.inf, customers.signup_t + 30.0)
    dropped = has_first & (rng.random(n) < p_drop)
    inactive_t[dropped] = t[dropped]
    alive = has_first & ~dropped

    while alive.any():
        idx = np.flatnonzero(alive)
        t[idx] += rng.exponential(1.0 / lam_max[idx])
        beyond = t[idx] >= horizon
        alive[idx[beyond]] = False
        idx = idx[~beyond]
        if not len(idx):
            break
        day = t[idx].astype(np.int64)
        lam = customers.rate[idx] / 365.0 * np.where(t[idx] >= change_t[idx], mult[idx], 1.0)
        accept = rng.random(len(idx)) < lam * demand[day] / lam_max[idx]
        acc = idx[accept]
        cust_parts.append(acc)
        time_parts.append(t[acc])
        counts[acc] += 1
        drop = rng.random(len(acc)) < p_drop[acc]
        alive[acc[drop]] = False
        inactive_t[acc[drop]] = t[acc[drop]]
        alive[counts >= MAX_ORDERS_PER_CUSTOMER] = False

    cust = np.concatenate(cust_parts)
    times = np.concatenate(time_parts)

    # Repeat orders get a realistic time of day; first orders keep their signup-session time.
    first_mask = np.zeros(len(cust), dtype=bool)
    first_mask[: len(cust_parts[0])] = True
    day = np.floor(times).astype(np.int64)
    secs = np.where(
        first_mask,
        (times * SECONDS_PER_DAY).astype(np.int64),
        day * SECONDS_PER_DAY + sample_seconds_of_day(rng, len(times)),
    )
    secs = np.maximum(secs, (customers.signup_t[cust] * SECONDS_PER_DAY).astype(np.int64) + 60)
    secs = np.minimum(secs, int(horizon * SECONDS_PER_DAY) - 1)

    order = np.lexsort((cust, secs))
    cust, secs = cust[order], secs[order]
    # Sequence number per customer (1 = first order)
    by_cust = np.lexsort((secs, cust))
    seq = np.empty(len(cust), dtype=np.int64)
    sorted_cust = cust[by_cust]
    starts = np.r_[True, sorted_cust[1:] != sorted_cust[:-1]]
    group_start = np.maximum.accumulate(np.where(starts, np.arange(len(cust)), 0))
    seq[by_cust] = np.arange(len(cust)) - group_start + 1

    order_ts = np.datetime64(profile.start, "s") + secs.astype("timedelta64[s]")
    return OrderSkeleton(
        order_id=np.array([f"ORD{i + 1:08d}" for i in range(len(cust))], dtype=object),
        cust_idx=cust,
        order_ts=order_ts,
        day_idx=(secs // SECONDS_PER_DAY).astype(np.int64),
        seq=seq,
        inactive_t=inactive_t,
    )
