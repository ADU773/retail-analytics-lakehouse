-- description: Order lines with validated quantities and conformed foreign keys (one row per order_item_id). Lines whose parent order was quarantined are quarantined too.
-- depends_on: bronze.order_items, silver.orders, quarantine.orders, silver.products
-- materialized: table
-- quarantine: quarantine.order_items

WITH typed AS (
    SELECT
        upper(nullif(trim(order_item_id), '')) AS order_item_id,
        upper(nullif(trim(order_id), ''))      AS order_id,
        upper(nullif(trim(product_id), ''))    AS product_id,
        to_int(quantity)                       AS quantity,
        to_money(unit_price)                   AS unit_price,
        coalesce(to_money(discount_amount), 0) AS discount_amount,
        parse_ts(created_at)                   AS created_at,
        quantity                               AS raw_quantity,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.order_items
),

validated AS (
    SELECT
        t.*,
        CASE
            WHEN t.order_item_id IS NULL OR t.order_id IS NULL THEN 'missing_key'
            WHEN t.quantity IS NULL OR t.quantity <= 0 THEN 'non_positive_quantity'
            WHEN t.unit_price IS NULL OR t.unit_price <= 0 THEN 'invalid_unit_price'
            WHEN o.order_id IS NULL AND q.order_id IS NOT NULL THEN 'parent_order_quarantined'
            WHEN o.order_id IS NULL THEN 'orphan_order_id'
            WHEN p.product_id IS NULL THEN 'orphan_product_id'
        END AS invalid_reason
    FROM typed AS t
    LEFT JOIN silver.orders AS o ON t.order_id = o.order_id
    LEFT JOIN (SELECT DISTINCT order_id FROM quarantine.orders) AS q ON t.order_id = q.order_id
    LEFT JOIN silver.products AS p ON t.product_id = p.product_id
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY order_item_id, invalid_reason IS NULL
            ORDER BY _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
