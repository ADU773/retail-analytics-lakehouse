"""Reference data: geography, marketing channels, the product catalog and name pools.

All brand and company names are fictional.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

# --------------------------------------------------------------------------------------
# Geography: region -> country -> cities (weights are relative)
# --------------------------------------------------------------------------------------
GEOGRAPHY: dict[str, dict[str, list[str]]] = {
    "North America": {
        "United States": ["New York", "Los Angeles", "Chicago", "Austin", "Seattle", "Denver"],
        "Canada": ["Toronto", "Vancouver", "Montreal"],
    },
    "Europe": {
        "United Kingdom": ["London", "Manchester", "Edinburgh"],
        "Germany": ["Berlin", "Munich", "Hamburg"],
        "France": ["Paris", "Lyon"],
        "Netherlands": ["Amsterdam", "Rotterdam"],
        "Spain": ["Madrid", "Barcelona"],
    },
    "Asia Pacific": {
        "Australia": ["Sydney", "Melbourne"],
        "Japan": ["Tokyo", "Osaka"],
        "Singapore": ["Singapore"],
    },
}
COUNTRY_WEIGHTS: dict[str, float] = {
    "United States": 0.46,
    "Canada": 0.09,
    "United Kingdom": 0.12,
    "Germany": 0.08,
    "France": 0.05,
    "Netherlands": 0.04,
    "Spain": 0.04,
    "Australia": 0.06,
    "Japan": 0.04,
    "Singapore": 0.02,
}
COUNTRY_TO_REGION = {c: r for r, cs in GEOGRAPHY.items() for c in cs}
COUNTRY_UTC_OFFSET = {  # used to render some timestamps in local time (DQ issue)
    "United States": -5,
    "Canada": -5,
    "United Kingdom": 0,
    "Germany": 1,
    "France": 1,
    "Netherlands": 1,
    "Spain": 1,
    "Australia": 10,
    "Japan": 9,
    "Singapore": 8,
}

# Flattened location table: one row per (region, country, city)
LOCATIONS: list[tuple[str, str, str]] = [
    (region, country, city)
    for region, countries in GEOGRAPHY.items()
    for country, cities in countries.items()
    for city in cities
]
LOCATION_WEIGHTS = np.array(
    [COUNTRY_WEIGHTS[c] / len(GEOGRAPHY[r][c]) for r, c, _ in LOCATIONS], dtype=float
)
LOCATION_WEIGHTS /= LOCATION_WEIGHTS.sum()

# --------------------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------------------
TRAFFIC_SOURCES = [
    "organic_search",
    "paid_search",
    "paid_social",
    "display",
    "email",
    "affiliate",
    "referral",
    "direct",
]
PAID_CHANNELS = ["paid_search", "paid_social", "display", "affiliate", "email"]
# Target ROAS used to size daily marketing spend relative to attributed revenue.
TARGET_ROAS = {
    "paid_search": 4.2,
    "paid_social": 2.4,
    "display": 0.9,
    "affiliate": 6.0,
    "email": 22.0,
}

ACQUISITION_CHANNELS = [
    "organic_search",
    "paid_search",
    "paid_social",
    "display",
    "affiliate",
    "referral",
    "direct",
]
ACQUISITION_WEIGHTS = np.array([0.26, 0.22, 0.20, 0.05, 0.09, 0.10, 0.08])
# Probability that a newly registered customer places a first order.
ACQUISITION_CONVERSION = np.array([0.74, 0.77, 0.60, 0.52, 0.80, 0.85, 0.80])
# Multiplier on the per-purchase dropout probability (customer quality by channel).
ACQUISITION_DROPOUT_MULT = np.array([0.95, 1.00, 1.30, 1.40, 1.05, 0.80, 0.90])

REPEAT_SOURCES = TRAFFIC_SOURCES
REPEAT_SOURCE_WEIGHTS = np.array([0.17, 0.12, 0.08, 0.02, 0.28, 0.04, 0.02, 0.27])
BROWSE_SOURCE_WEIGHTS = np.array([0.30, 0.18, 0.20, 0.07, 0.08, 0.04, 0.05, 0.08])

DEVICES = ["desktop", "mobile", "tablet"]
PURCHASE_DEVICE_WEIGHTS = np.array([0.46, 0.42, 0.12])
BROWSE_DEVICE_WEIGHTS = np.array([0.30, 0.60, 0.10])
# Non-purchase funnel progression probabilities per device:
# P(product_view), P(add_to_cart | view), P(checkout | cart)
FUNNEL_PROBS = {
    "desktop": (0.72, 0.26, 0.48),
    "mobile": (0.60, 0.17, 0.36),
    "tablet": (0.66, 0.22, 0.42),
}
SOURCE_ENGAGEMENT = {  # multiplies P(add_to_cart | view)
    "organic_search": 1.00,
    "paid_search": 1.10,
    "paid_social": 0.70,
    "display": 0.55,
    "email": 1.35,
    "affiliate": 1.15,
    "referral": 1.10,
    "direct": 1.25,
}

# --------------------------------------------------------------------------------------
# Customer segments (loyalty tier). Rates are purchases per year for an active customer.
# --------------------------------------------------------------------------------------
SEGMENTS = ["standard", "plus", "business"]
SEGMENT_WEIGHTS = np.array([0.72, 0.18, 0.10])
SEGMENT_PURCHASE_RATE = np.array([2.4, 5.5, 3.8])
SEGMENT_RATE_SHAPE = np.array([1.6, 2.5, 2.0])  # gamma heterogeneity of individual rates
SEGMENT_DROPOUT_BETA = np.array([[2.0, 5.0], [1.2, 9.0], [1.5, 7.5]])  # Beta(a, b) per segment
SEGMENT_BASKET_EXTRA = np.array([0.7, 0.9, 1.6])  # Poisson mean of extra lines per order
SEGMENT_RETURN_MULT = np.array([1.0, 0.9, 0.6])

PAYMENT_METHODS = ["card", "paypal", "apple_pay", "gift_card"]
PAYMENT_WEIGHTS = np.array([0.62, 0.20, 0.12, 0.06])

RETURN_REASONS_APPAREL = (
    ["wrong_size", "not_as_described", "changed_mind", "damaged", "late_delivery"],
    np.array([0.45, 0.20, 0.20, 0.10, 0.05]),
)
RETURN_REASONS_OTHER = (
    ["damaged", "defective", "not_as_described", "changed_mind", "late_delivery"],
    np.array([0.30, 0.25, 0.20, 0.20, 0.05]),
)

FIRST_NAMES = [
    "Olivia",
    "Liam",
    "Emma",
    "Noah",
    "Ava",
    "Elijah",
    "Sophia",
    "Lucas",
    "Mia",
    "Mateo",
    "Amelia",
    "Leo",
    "Harper",
    "Ethan",
    "Ella",
    "Aarav",
    "Isla",
    "Arjun",
    "Chloe",
    "Hugo",
    "Zoe",
    "Felix",
    "Nora",
    "Oscar",
    "Lena",
    "Jonas",
    "Clara",
    "Theo",
    "Maya",
    "Kenji",
    "Yuki",
    "Hana",
    "Ren",
    "Sakura",
    "Priya",
    "Rohan",
    "Ananya",
    "Diego",
    "Lucia",
    "Pablo",
    "Sofia",
    "Marco",
    "Giulia",
    "Lars",
    "Freya",
    "Anouk",
    "Daan",
    "Emily",
    "Jack",
    "Grace",
    "Henry",
    "Lily",
    "Samuel",
    "Aisha",
    "Omar",
    "Layla",
    "Wei",
    "Mei",
    "Jun",
    "Chen",
    "Ines",
    "Rafael",
    "Elena",
    "Victor",
]
LAST_NAMES = [
    "Smith",
    "Johnson",
    "Brown",
    "Garcia",
    "Miller",
    "Davis",
    "Martinez",
    "Lopez",
    "Wilson",
    "Anderson",
    "Taylor",
    "Thomas",
    "Moore",
    "Martin",
    "Lee",
    "Clark",
    "Lewis",
    "Walker",
    "Hall",
    "Young",
    "King",
    "Wright",
    "Scott",
    "Green",
    "Baker",
    "Adams",
    "Nelson",
    "Hill",
    "Campbell",
    "Mitchell",
    "Roberts",
    "Carter",
    "Phillips",
    "Evans",
    "Turner",
    "Parker",
    "Collins",
    "Edwards",
    "Stewart",
    "Morris",
    "Murphy",
    "Cook",
    "Rogers",
    "Morgan",
    "Cooper",
    "Peterson",
    "Reed",
    "Bailey",
    "Bell",
    "Kelly",
    "Howard",
    "Ward",
    "Cox",
    "Rivera",
    "Richardson",
    "Wood",
    "Watson",
    "Brooks",
    "Bennett",
    "Gray",
    "Patel",
    "Shah",
    "Nair",
    "Tanaka",
    "Sato",
    "Suzuki",
    "Mueller",
    "Schmidt",
    "Fischer",
    "Weber",
    "Dubois",
    "Moreau",
    "Laurent",
    "Jansen",
    "DeVries",
    "Bakker",
    "Fernandez",
    "Romero",
    "Navarro",
    "Kim",
    "Park",
]
EMAIL_DOMAINS = ["example.com", "example.org", "example.net", "mail.example"]

# --------------------------------------------------------------------------------------
# Product catalog: category > subcategory with (min, max) list price
# --------------------------------------------------------------------------------------
CATEGORY_TREE: dict[str, dict[str, tuple[float, float]]] = {
    "Home & Kitchen": {
        "Cookware": (28, 190),
        "Coffee & Tea": (12, 150),
        "Tableware": (14, 95),
        "Storage": (9, 60),
    },
    "Apparel": {
        "Tops": (22, 75),
        "Bottoms": (34, 110),
        "Outerwear": (85, 260),
        "Accessories": (12, 65),
    },
    "Outdoor": {"Camping": (25, 340), "Hiking": (20, 190), "Cycling": (15, 160)},
    "Beauty & Wellness": {"Skincare": (12, 80), "Bath & Body": (8, 45), "Fitness": (15, 130)},
    "Electronics": {"Audio": (35, 280), "Smart Home": (25, 190), "Charging": (12, 75)},
}
BRANDS: dict[str, list[str]] = {
    "Home & Kitchen": ["Aurora Home", "Cedarwick", "Halcyra"],
    "Apparel": ["Velmora", "Quillfern", "Aurora Basics"],
    "Outdoor": ["Trailhaven", "Brightmoss"],
    "Beauty & Wellness": ["Lumio", "Solenne"],
    "Electronics": ["Voltaro", "Lumio Labs"],
}
PRODUCT_NOUNS: dict[str, list[str]] = {
    "Cookware": ["Skillet", "Dutch Oven", "Saucepan", "Wok", "Stockpot", "Sheet Pan"],
    "Coffee & Tea": [
        "Pour-Over Kit",
        "French Press",
        "Burr Grinder",
        "Tea Kettle",
        "Espresso Cups",
    ],
    "Tableware": ["Dinner Plates", "Pasta Bowls", "Glass Tumblers", "Serving Board", "Mug Set"],
    "Storage": ["Pantry Jars", "Lunch Box", "Spice Rack", "Bread Bin"],
    "Tops": ["Linen Shirt", "Merino Tee", "Crewneck Sweater", "Oxford Shirt", "Polo"],
    "Bottoms": ["Chinos", "Denim Jeans", "Joggers", "Shorts"],
    "Outerwear": ["Rain Jacket", "Puffer Jacket", "Wool Coat", "Fleece"],
    "Accessories": ["Beanie", "Scarf", "Tote Bag", "Leather Belt", "Socks 3-Pack"],
    "Camping": ["2-Person Tent", "Sleeping Bag", "Camp Stove", "Lantern", "Camp Chair"],
    "Hiking": ["Daypack", "Trekking Poles", "Water Bottle", "Trail Socks", "Headlamp"],
    "Cycling": ["Bike Light Set", "Helmet", "Saddle Bag", "Bike Lock"],
    "Skincare": ["Face Serum", "Moisturiser", "Cleanser", "SPF 50 Lotion"],
    "Bath & Body": ["Body Wash", "Bath Salts", "Hand Cream", "Candle"],
    "Fitness": ["Yoga Mat", "Resistance Bands", "Foam Roller", "Kettlebell"],
    "Audio": ["Wireless Earbuds", "Bluetooth Speaker", "Headphones", "Turntable"],
    "Smart Home": ["Smart Bulb", "Smart Plug", "Video Doorbell", "Thermostat"],
    "Charging": ["USB-C Charger", "Power Bank", "Charging Stand", "Cable Kit"],
}
ADJECTIVES = ["Classic", "Everyday", "Pro", "Nordic", "Essential", "Signature", "Studio", "Air"]
COMPLEMENTS: dict[str, list[str]] = {
    "Cookware": ["Storage", "Tableware"],
    "Coffee & Tea": ["Tableware"],
    "Tableware": ["Coffee & Tea"],
    "Storage": ["Cookware"],
    "Tops": ["Bottoms", "Accessories"],
    "Bottoms": ["Tops"],
    "Outerwear": ["Accessories"],
    "Accessories": ["Outerwear", "Tops"],
    "Camping": ["Hiking"],
    "Hiking": ["Camping"],
    "Cycling": ["Charging"],
    "Skincare": ["Bath & Body"],
    "Bath & Body": ["Skincare"],
    "Fitness": ["Audio"],
    "Audio": ["Charging"],
    "Smart Home": ["Charging"],
    "Charging": ["Audio", "Smart Home"],
}
CATEGORY_RETURN_RATE = {
    "Apparel": 0.13,
    "Electronics": 0.07,
    "Outdoor": 0.05,
    "Home & Kitchen": 0.04,
    "Beauty & Wellness": 0.03,
}
# Month-of-year category demand multipliers (index 0 = January).
CATEGORY_SEASONALITY: dict[str, list[float]] = {
    "Outdoor": [0.6, 0.6, 0.8, 1.1, 1.4, 1.6, 1.6, 1.4, 1.1, 0.8, 0.7, 0.8],
    "Electronics": [0.9, 0.8, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9, 1.0, 1.0, 1.4, 1.6],
    "Apparel": [1.0, 0.9, 1.0, 1.1, 1.0, 0.9, 0.9, 1.0, 1.1, 1.2, 1.2, 1.1],
}


@dataclass
class ProductTable:
    """Columnar product catalog used by the generator."""

    product_id: np.ndarray
    name: np.ndarray
    category: np.ndarray
    subcategory: np.ndarray
    brand: np.ndarray
    list_price: np.ndarray
    unit_cost: np.ndarray
    launched_at: np.ndarray  # datetime64[s]
    popularity: np.ndarray
    partner: np.ndarray  # index of the "frequently bought together" partner, -1 if none
    return_rate: np.ndarray

    def __len__(self) -> int:
        return len(self.product_id)


def _price_point(raw: float) -> float:
    """Round to a retail price point ending in .99 / .00 / .50."""
    whole = max(1.0, round(raw))
    return float(whole - 0.01) if whole % 5 else float(whole)


def build_products(rng: np.random.Generator, start: date, end: date) -> ProductTable:
    """Build the product catalog (deterministic for a given ``rng`` state)."""
    rows: list[dict[str, object]] = []
    horizon_days = (end - start).days
    for category, subcats in CATEGORY_TREE.items():
        for subcategory, (lo, hi) in subcats.items():
            nouns = PRODUCT_NOUNS[subcategory]
            n_items = len(nouns) + int(rng.integers(2, 5))
            for i in range(n_items):
                noun = nouns[i % len(nouns)]
                brand = BRANDS[category][int(rng.integers(len(BRANDS[category])))]
                adjective = ADJECTIVES[int(rng.integers(len(ADJECTIVES)))]
                price = _price_point(float(np.exp(rng.uniform(np.log(lo), np.log(hi)))))
                margin = rng.uniform(0.38, 0.62)
                if rng.random() < 0.75:
                    launched = start - timedelta(days=int(rng.integers(30, 900)))
                else:
                    launched = start + timedelta(days=int(rng.integers(30, max(31, horizon_days))))
                rows.append(
                    {
                        "name": f"{brand} {adjective} {noun}",
                        "category": category,
                        "subcategory": subcategory,
                        "brand": brand,
                        "list_price": price,
                        "unit_cost": round(price * (1 - margin), 2),
                        "launched_at": np.datetime64(launched, "s")
                        + np.timedelta64(int(rng.integers(8 * 3600, 18 * 3600)), "s"),
                        "popularity": float(rng.lognormal(0.0, 0.8)),
                    }
                )
    n = len(rows)
    subcategory = np.array([r["subcategory"] for r in rows], dtype=object)
    category = np.array([r["category"] for r in rows], dtype=object)

    # Each product gets a "frequently bought together" partner in a complementary subcategory.
    partner = np.full(n, -1, dtype=np.int64)
    for i in range(n):
        comps = COMPLEMENTS.get(str(subcategory[i]), [])
        candidates = np.flatnonzero(np.isin(subcategory, comps))
        if len(candidates):
            partner[i] = int(rng.choice(candidates))

    return ProductTable(
        product_id=np.array([f"P{i + 1:04d}" for i in range(n)], dtype=object),
        name=np.array([r["name"] for r in rows], dtype=object),
        category=category,
        subcategory=subcategory,
        brand=np.array([r["brand"] for r in rows], dtype=object),
        list_price=np.array([r["list_price"] for r in rows], dtype=float),
        unit_cost=np.array([r["unit_cost"] for r in rows], dtype=float),
        launched_at=np.array([r["launched_at"] for r in rows], dtype="datetime64[s]"),
        popularity=np.array([r["popularity"] for r in rows], dtype=float),
        partner=partner,
        return_rate=np.array([CATEGORY_RETURN_RATE[str(c)] for c in category], dtype=float),
    )


def category_month_weights(products: ProductTable, month: int) -> np.ndarray:
    """Product sampling weights for a calendar month (popularity x category seasonality)."""
    mult = np.array(
        [CATEGORY_SEASONALITY.get(str(c), [1.0] * 12)[month - 1] for c in products.category]
    )
    outerwear = products.subcategory == "Outerwear"
    if month in (10, 11, 12, 1, 2):
        mult = np.where(outerwear, mult * 1.8, mult)
    elif month in (5, 6, 7, 8):
        mult = np.where(outerwear, mult * 0.5, mult)
    return products.popularity * mult
