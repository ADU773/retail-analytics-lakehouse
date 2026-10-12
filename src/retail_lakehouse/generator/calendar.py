"""Retail calendar: weekly/annual seasonality, Black Friday & holiday peaks, promotions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

# Day-of-week demand factors (Monday=0). DTC traffic peaks on Sunday evenings.
WEEKDAY_FACTORS = np.array([1.00, 0.92, 0.93, 0.95, 0.98, 1.08, 1.18])
WEEKDAY_FACTORS = WEEKDAY_FACTORS / WEEKDAY_FACTORS.mean()

# Mid-month annual seasonality anchors (Jan..Dec), interpolated daily.
MONTH_FACTORS = np.array([0.84, 0.86, 0.95, 1.00, 1.00, 0.95, 0.97, 1.00, 0.97, 1.03, 1.15, 1.28])

ACQUISITION_GROWTH_PER_YEAR = 0.35  # new-customer acquisition grows ~35% YoY


@dataclass(frozen=True)
class Promotion:
    """A dated promotion with a discount code."""

    code: str
    name: str
    start: date
    end: date
    discount_pct: float
    uplift: float
    usage_prob: float


def thanksgiving(year: int) -> date:
    """US Thanksgiving: the fourth Thursday of November."""
    nov1 = date(year, 11, 1)
    first_thursday = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    return first_thursday + timedelta(days=21)


def black_friday(year: int) -> date:
    """Black Friday: the day after US Thanksgiving."""
    return thanksgiving(year) + timedelta(days=1)


def cyber_monday(year: int) -> date:
    """Cyber Monday: the Monday after Black Friday."""
    return black_friday(year) + timedelta(days=3)


def build_promotions(start: date, end: date) -> list[Promotion]:
    """Promotional calendar covering every year that intersects ``[start, end]``."""
    promos: list[Promotion] = []
    for year in range(start.year, end.year + 1):
        bf = black_friday(year)
        yy = str(year)[2:]
        promos += [
            Promotion(
                f"NEWYEAR{yy}",
                "New Year Sale",
                date(year, 1, 2),
                date(year, 1, 8),
                0.10,
                1.15,
                0.45,
            ),
            Promotion(
                f"SPRING{yy}",
                "Spring Refresh",
                date(year, 4, 10),
                date(year, 4, 16),
                0.15,
                1.25,
                0.50,
            ),
            Promotion(
                f"SUMMER{yy}",
                "Aurora Summer Days",
                date(year, 7, 15),
                date(year, 7, 21),
                0.20,
                1.35,
                0.55,
            ),
            Promotion(
                f"BFCM{yy}",
                "Black Friday / Cyber Monday",
                bf,
                bf + timedelta(days=3),
                0.30,
                1.00,
                0.75,
            ),
            Promotion(
                f"HOLIDAY{yy}",
                "Holiday Gift Guide",
                date(year, 12, 1),
                date(year, 12, 18),
                0.15,
                1.00,
                0.40,
            ),
        ]
    return [p for p in promos if p.end >= start and p.start <= end]


def _annual_curve(day_of_year: np.ndarray) -> np.ndarray:
    """Smooth annual seasonality by interpolating monthly anchors (wrap-around)."""
    anchors = np.array([15.5 + 30.44 * i for i in range(12)])
    xs = np.concatenate([anchors - 365.25, anchors, anchors + 365.25])
    ys = np.tile(MONTH_FACTORS, 3)
    return np.interp(day_of_year, xs, ys)


def _event_factor(d: date) -> tuple[float, str]:
    """Multiplicative demand factor and label for retail events."""
    bf = black_friday(d.year)
    cm = cyber_monday(d.year)
    if d == bf:
        return 3.2, "Black Friday"
    if d == cm:
        return 2.6, "Cyber Monday"
    if bf < d < cm:
        return 1.8, "Black Friday Weekend"
    if bf - timedelta(days=4) <= d < bf - timedelta(days=1):
        return 1.3, "Black Friday Week"
    if d == bf - timedelta(days=1):
        return 0.9, "Thanksgiving"
    if d.month == 12:
        if d.day <= 18:
            return 1.15, "Holiday Shopping"
        if d.day <= 23:
            return 1.05, "Holiday Shipping Cutoff"
        if d.day == 24:
            return 0.7, "Christmas Eve"
        if d.day == 25:
            return 0.45, "Christmas Day"
        if d.day == 26:
            return 1.6, "Boxing Day"
        return 1.2, "Year-End Sale"
    return 1.0, ""


@dataclass
class DailyProfile:
    """Per-day multipliers for the generated date range (index 0 == ``start``)."""

    start: date
    end: date
    dates: np.ndarray  # datetime64[D]
    demand: np.ndarray  # per-customer purchase-rate multiplier
    acquisition: np.ndarray  # relative new-customer signups per day
    traffic: np.ndarray  # relative anonymous web traffic per day
    promo_index: np.ndarray  # index into ``promotions`` or -1
    event_label: list[str]
    promotions: list[Promotion]

    @property
    def n_days(self) -> int:
        return len(self.dates)

    @property
    def max_demand(self) -> float:
        return float(self.demand.max())

    def day_index(self, d: date) -> int:
        return (d - self.start).days

    def month_starts(self) -> list[date]:
        """First day of every calendar month in range (the first may be mid-month)."""
        months: list[date] = []
        d = date(self.start.year, self.start.month, 1)
        while d <= self.end:
            months.append(max(d, self.start))
            d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
        return months


def build_daily_profile(start: date, end: date) -> DailyProfile:
    """Compute daily seasonality, event and promotion multipliers for ``[start, end]``."""
    n_days = (end - start).days + 1
    dates = np.arange(
        np.datetime64(start), np.datetime64(end) + np.timedelta64(1, "D"), dtype="datetime64[D]"
    )
    pydates = [start + timedelta(days=i) for i in range(n_days)]
    weekday = np.array([d.weekday() for d in pydates])
    doy = np.array([d.timetuple().tm_yday for d in pydates], dtype=float)

    events = [_event_factor(d) for d in pydates]
    event_mult = np.array([e[0] for e in events])
    labels = [e[1] for e in events]

    promotions = build_promotions(start, end)
    promo_index = np.full(n_days, -1, dtype=np.int64)
    promo_uplift = np.ones(n_days)
    for idx, promo in enumerate(promotions):
        lo = max(0, (promo.start - start).days)
        hi = min(n_days - 1, (promo.end - start).days)
        if lo <= hi:
            promo_index[lo : hi + 1] = idx
            promo_uplift[lo : hi + 1] = promo.uplift
            for i in range(lo, hi + 1):
                if not labels[i]:
                    labels[i] = promo.name

    annual = _annual_curve(doy)
    demand = WEEKDAY_FACTORS[weekday] * annual * event_mult * promo_uplift

    years_elapsed = np.arange(n_days) / 365.25
    trend = (1.0 + ACQUISITION_GROWTH_PER_YEAR) ** years_elapsed
    # Acquisition responds to events/promos more softly than purchase demand.
    acquisition = trend * WEEKDAY_FACTORS[weekday] * annual * np.sqrt(event_mult * promo_uplift)
    traffic = trend * WEEKDAY_FACTORS[weekday] * annual * np.sqrt(event_mult) * promo_uplift
    return DailyProfile(
        start=start,
        end=end,
        dates=dates,
        demand=demand,
        acquisition=acquisition,
        traffic=traffic,
        promo_index=promo_index,
        event_label=labels,
        promotions=promotions,
    )
