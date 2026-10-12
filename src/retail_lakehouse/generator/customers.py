"""Customer population: attributes, latent purchase behaviour and attribute changes over time.

Each customer has a latent purchase rate (Gamma-distributed around a segment mean) and a
per-purchase dropout probability (Beta-distributed), which drives repeat purchasing and
churn. A minority of customers change loyalty tier (``segment``) or move to a new city/
country during the period; those changes are emitted as CDC-style snapshot records and
become SCD Type 2 history in ``gold.dim_customer``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from retail_lakehouse.generator import catalog
from retail_lakehouse.generator.calendar import DailyProfile

SECONDS_PER_DAY = 86_400
NAT = np.datetime64("NaT", "s")

# Hour-of-day weights for signups and orders (UTC, blended across regions).
HOURLY_WEIGHTS = np.array(
    [
        1.4,
        1.0,
        0.8,
        0.6,
        0.5,
        0.6,
        1.0,
        1.6,
        2.2,
        2.6,
        2.9,
        3.1,
        3.4,
        3.3,
        3.1,
        3.0,
        3.1,
        3.4,
        3.9,
        4.4,
        4.6,
        4.2,
        3.3,
        2.3,
    ]
)
HOURLY_WEIGHTS = HOURLY_WEIGHTS / HOURLY_WEIGHTS.sum()


def sample_seconds_of_day(rng: np.random.Generator, size: int) -> np.ndarray:
    """Seconds after midnight following the hourly traffic profile."""
    hours = rng.choice(24, size=size, p=HOURLY_WEIGHTS)
    return hours * 3600 + rng.integers(0, 3600, size=size)


@dataclass
class CustomerTable:
    """Columnar customer population (index ``i`` is the customer's row everywhere)."""

    customer_id: np.ndarray
    first_name: np.ndarray
    last_name: np.ndarray
    email: np.ndarray
    signup_ts: np.ndarray  # datetime64[s]
    signup_t: np.ndarray  # float days since profile.start
    acquisition_idx: np.ndarray
    location_idx: np.ndarray
    segment_idx: np.ndarray
    marketing_opt_in: np.ndarray
    rate: np.ndarray  # purchases / year while active
    dropout_p: np.ndarray  # probability of churning after each purchase
    converts: np.ndarray  # places a first order at all
    tier_change_ts: np.ndarray  # datetime64[s] or NaT
    tier_new_segment: np.ndarray
    tier_rate_mult: np.ndarray  # rate multiplier after the tier change
    move_ts: np.ndarray
    move_location_idx: np.ndarray
    email_change_ts: np.ndarray
    new_email: np.ndarray
    visitor_id: np.ndarray

    def __len__(self) -> int:
        return len(self.customer_id)

    def tier_change_t(self, start: np.datetime64) -> np.ndarray:
        """Tier-change time in float days since ``start`` (inf when no change)."""
        delta = (self.tier_change_ts - start).astype("timedelta64[s]").astype(float)
        out = delta / SECONDS_PER_DAY
        out[np.isnat(self.tier_change_ts)] = np.inf
        return out

    def state_at(self, idx: np.ndarray, ts: np.ndarray) -> dict[str, np.ndarray]:
        """Segment, location and email for customers ``idx`` as of timestamps ``ts``."""
        tier = ~np.isnat(self.tier_change_ts[idx]) & (self.tier_change_ts[idx] <= ts)
        moved = ~np.isnat(self.move_ts[idx]) & (self.move_ts[idx] <= ts)
        mail = ~np.isnat(self.email_change_ts[idx]) & (self.email_change_ts[idx] <= ts)
        return {
            "segment_idx": np.where(tier, self.tier_new_segment[idx], self.segment_idx[idx]),
            "location_idx": np.where(moved, self.move_location_idx[idx], self.location_idx[idx]),
            "email": np.where(mail, self.new_email[idx], self.email[idx]),
        }


def _make_email(rng: np.random.Generator, first: np.ndarray, last: np.ndarray) -> np.ndarray:
    seps = rng.choice([".", "_", ""], size=len(first), p=[0.6, 0.15, 0.25])
    nums = rng.integers(1, 9999, size=len(first))
    domains = rng.choice(catalog.EMAIL_DOMAINS, size=len(first))
    return np.array(
        [
            f"{f.lower()}{s}{la.lower()}{n}@{d}"
            for f, s, la, n, d in zip(first, seps, last, nums, domains, strict=True)
        ],
        dtype=object,
    )


def _random_ts_after(
    rng: np.random.Generator, base: np.ndarray, mean_days: float, end: np.datetime64
) -> np.ndarray:
    """``base`` + exponential delay; NaT when the event falls after ``end``."""
    delay = (rng.exponential(mean_days, size=len(base)) * SECONDS_PER_DAY).astype(np.int64)
    ts = base + delay.astype("timedelta64[s]")
    return np.where(ts <= end, ts, NAT)


def build_customers(
    rng: np.random.Generator, n_customers: int, profile: DailyProfile
) -> CustomerTable:
    """Generate ``n_customers`` customers with signups spread over the profile period."""
    n = n_customers
    start = np.datetime64(profile.start, "s")
    end = np.datetime64(profile.end, "s") + np.timedelta64(SECONDS_PER_DAY - 1, "s")

    p_day = profile.acquisition / profile.acquisition.sum()
    day = rng.choice(profile.n_days, size=n, p=p_day)
    secs = day.astype(np.int64) * SECONDS_PER_DAY + sample_seconds_of_day(rng, n)
    order = np.argsort(secs, kind="stable")  # customer ids follow signup order
    secs = secs[order]
    signup_ts = start + secs.astype("timedelta64[s]")

    acquisition_idx = rng.choice(
        len(catalog.ACQUISITION_CHANNELS), size=n, p=catalog.ACQUISITION_WEIGHTS
    )
    location_idx = rng.choice(len(catalog.LOCATIONS), size=n, p=catalog.LOCATION_WEIGHTS)
    segment_idx = rng.choice(len(catalog.SEGMENTS), size=n, p=catalog.SEGMENT_WEIGHTS)

    first = rng.choice(catalog.FIRST_NAMES, size=n).astype(object)
    last = rng.choice(catalog.LAST_NAMES, size=n).astype(object)
    email = _make_email(rng, first, last)

    shape = catalog.SEGMENT_RATE_SHAPE[segment_idx]
    rate = rng.gamma(shape, catalog.SEGMENT_PURCHASE_RATE[segment_idx] / shape)
    rate = np.clip(rate, 0.15, 30.0)
    beta = catalog.SEGMENT_DROPOUT_BETA[segment_idx]
    dropout = rng.beta(beta[:, 0], beta[:, 1]) * catalog.ACQUISITION_DROPOUT_MULT[acquisition_idx]
    dropout = np.clip(dropout, 0.01, 0.95)
    converts = rng.random(n) < catalog.ACQUISITION_CONVERSION[acquisition_idx]

    # --- loyalty tier changes (SCD2): standard -> plus upgrades, plus -> standard downgrades
    tier_change_ts = np.full(n, NAT)
    tier_new = segment_idx.copy()
    up = (segment_idx == 0) & (rng.random(n) < 0.12)
    down = (segment_idx == 1) & (rng.random(n) < 0.14)
    tier_change_ts[up] = _random_ts_after(rng, signup_ts[up], 220.0, end)
    tier_change_ts[down] = _random_ts_after(rng, signup_ts[down], 300.0, end)
    tier_new[up] = 1
    tier_new[down] = 0
    has_tier = ~np.isnat(tier_change_ts)
    tier_new = np.where(has_tier, tier_new, segment_idx)
    tier_mult = np.where(
        has_tier,
        catalog.SEGMENT_PURCHASE_RATE[tier_new] / catalog.SEGMENT_PURCHASE_RATE[segment_idx],
        1.0,
    )

    # --- relocations (SCD2 on region/country/city)
    move = rng.random(n) < 0.05
    move_ts = np.full(n, NAT)
    delay = rng.uniform(45, 700, size=n) * SECONDS_PER_DAY
    candidate = signup_ts + delay.astype(np.int64).astype("timedelta64[s]")
    move_ts[move] = np.where(candidate[move] <= end, candidate[move], NAT)
    move_loc = rng.choice(len(catalog.LOCATIONS), size=n, p=catalog.LOCATION_WEIGHTS)
    same = move_loc == location_idx
    move_loc[same] = (move_loc[same] + 1) % len(catalog.LOCATIONS)

    # --- e-mail updates (type 1 attribute: overwritten, not versioned)
    mail = rng.random(n) < 0.03
    email_change_ts = np.full(n, NAT)
    email_change_ts[mail] = _random_ts_after(rng, signup_ts[mail], 200.0, end)
    new_email = _make_email(rng, first, last)

    visitor_id = np.array(
        [f"V{(i * 2654435761 + 97) % (1 << 32):08x}{i % 997:03d}" for i in range(n)], dtype=object
    )
    return CustomerTable(
        customer_id=np.array([f"C{i + 1:07d}" for i in range(n)], dtype=object),
        first_name=first,
        last_name=last,
        email=email,
        signup_ts=signup_ts,
        signup_t=secs / SECONDS_PER_DAY,
        acquisition_idx=acquisition_idx,
        location_idx=location_idx,
        segment_idx=segment_idx,
        marketing_opt_in=rng.random(n) < 0.62,
        rate=rate,
        dropout_p=dropout,
        converts=converts,
        tier_change_ts=tier_change_ts,
        tier_new_segment=tier_new,
        tier_rate_mult=tier_mult,
        move_ts=move_ts,
        move_location_idx=move_loc,
        email_change_ts=email_change_ts,
        new_email=new_email,
        visitor_id=visitor_id,
    )


def to_datetime(ts: np.datetime64) -> datetime:
    """numpy datetime64[s] -> naive ``datetime``."""
    return ts.astype("datetime64[s]").astype(datetime)
