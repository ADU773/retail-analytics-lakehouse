-- description: Product returns with standardised reasons, validated against the purchased order line (one row per return_id).
-- depends_on: bronze.returns, silver.order_items
-- materialized: table
-- quarantine: quarantine.returns

WITH typed AS (
    SELECT
        upper(nullif(trim(return_id), ''))     AS return_id,
        upper(nullif(trim(order_item_id), '')) AS order_item_id,
        upper(nullif(trim(order_id), ''))      AS order_id,
        upper(nullif(trim(product_id), ''))    AS product_id,
        to_int(quantity)                       AS quantity,
        to_money(refund_amount)                AS refund_amount,
        clean_code(reason)                     AS reason,
        parse_ts(requested_at)                 AS requested_at,
        clean_code(status)                     AS status,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.returns
),

validated AS (
    SELECT
        t.*,
        CASE
            WHEN t.return_id IS NULL THEN 'missing_return_id'
            WHEN t.requested_at IS NULL THEN 'unparseable_requested_at'
            WHEN i.order_item_id IS NULL THEN 'orphan_order_item_id'
            WHEN t.quantity IS NULL OR t.quantity <= 0 THEN 'non_positive_quantity'
            WHEN t.quantity > i.quantity THEN 'quantity_exceeds_purchase'
            WHEN t.reason NOT IN (
                'wrong_size', 'not_as_described', 'changed_mind', 'damaged',
                'defective', 'late_delivery'
            ) THEN 'invalid_reason'
        END AS invalid_reason
    FROM typed AS t
    LEFT JOIN silver.order_items AS i ON t.order_item_id = i.order_item_id
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY return_id, invalid_reason IS NULL
            ORDER BY _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
