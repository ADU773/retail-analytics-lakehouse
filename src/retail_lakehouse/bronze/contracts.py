"""Raw landing-zone contracts: file format, expected columns and partition column per entity."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntityContract:
    """Expected shape of one raw entity.

    Bronze keeps every source column as ``VARCHAR`` (raw fidelity); typing happens in silver.
    ``partition_column`` is parsed leniently to derive the ``year=/month=`` partition.
    """

    name: str
    fmt: str  # "csv" | "jsonl"
    columns: tuple[str, ...]
    partition_column: str
    description: str

    @property
    def extension(self) -> str:
        return self.fmt


CONTRACTS: dict[str, EntityContract] = {
    c.name: c
    for c in [
        EntityContract(
            "customers",
            "jsonl",
            (
                "customer_id",
                "first_name",
                "last_name",
                "email",
                "signup_ts",
                "acquisition_channel",
                "region",
                "country",
                "city",
                "segment",
                "marketing_opt_in",
                "updated_at",
            ),
            "updated_at",
            "CRM export: new sign-ups plus CDC snapshots when a customer changes",
        ),
        EntityContract(
            "products",
            "csv",
            (
                "product_id",
                "product_name",
                "category",
                "subcategory",
                "brand",
                "list_price",
                "unit_cost",
                "launched_at",
            ),
            "launched_at",
            "Product information management (PIM) catalog extract",
        ),
        EntityContract(
            "orders",
            "csv",
            (
                "order_id",
                "customer_id",
                "order_ts",
                "status",
                "payment_method",
                "discount_code",
                "shipping_fee",
                "order_total",
                "currency",
                "updated_at",
            ),
            "order_ts",
            "OLTP order headers (with re-sent CDC versions)",
        ),
        EntityContract(
            "order_items",
            "csv",
            (
                "order_item_id",
                "order_id",
                "product_id",
                "quantity",
                "unit_price",
                "discount_amount",
                "created_at",
            ),
            "created_at",
            "OLTP order lines",
        ),
        EntityContract(
            "returns",
            "jsonl",
            (
                "return_id",
                "order_item_id",
                "order_id",
                "product_id",
                "quantity",
                "refund_amount",
                "reason",
                "requested_at",
                "status",
            ),
            "requested_at",
            "Returns-portal webhook events",
        ),
        EntityContract(
            "web_sessions",
            "csv",
            (
                "session_id",
                "visitor_id",
                "customer_id",
                "session_start",
                "device",
                "traffic_source",
                "country",
            ),
            "session_start",
            "Web analytics sessions (anonymous and logged-in)",
        ),
        EntityContract(
            "web_events",
            "csv",
            ("event_id", "session_id", "event_type", "event_ts", "product_id", "order_id"),
            "event_ts",
            "Funnel events: product_view, add_to_cart, checkout, purchase",
        ),
        EntityContract(
            "marketing_spend",
            "jsonl",
            ("date", "channel", "spend", "impressions", "clicks", "currency"),
            "date",
            "Daily paid-media spend per channel from ad-platform APIs",
        ),
    ]
}

LINEAGE_COLUMNS = ("_ingested_at", "_source_file", "_batch_id", "_run_id")
