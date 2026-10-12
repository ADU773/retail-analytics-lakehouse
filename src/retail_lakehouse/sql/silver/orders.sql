-- description: Order headers normalised to UTC, de-duplicated to the latest CDC version per order_id (window function on updated_at), with late-arrival flags.
-- depends_on: bronze.orders, silver.customers
-- materialized: table
-- quarantine: quarantine.orders

WITH typed AS (
    SELECT
        upper(nullif(trim(order_id), ''))      AS order_id,
        upper(nullif(trim(customer_id), ''))   AS customer_id,
        parse_ts(order_ts)                     AS order_ts,
        clean_code(status)                     AS status,
        clean_code(payment_method)             AS payment_method,
        upper(nullif(trim(discount_code), '')) AS discount_code,
        to_money(shipping_fee)                 AS shipping_fee,
        to_money(order_total)                  AS order_total,
        upper(trim(currency))                  AS currency,
        parse_ts(updated_at)                   AS updated_at,
        CASE
            WHEN regexp_full_match(trim(order_ts), '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z') THEN 'iso_utc'
            WHEN regexp_full_match(trim(order_ts), '\d{9,11}') THEN 'epoch_seconds'
            WHEN regexp_full_match(trim(order_ts), '\d{1,2}/\d{1,2}/\d{4} .*') THEN 'us_local_format'
            WHEN regexp_matches(trim(order_ts), '[+-]\d{2}:?\d{2}$') THEN 'iso_with_offset'
            ELSE 'other'
        END                                    AS order_ts_format,
        order_ts                               AS raw_order_ts,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.orders
),

validated AS (
    SELECT
        t.*,
        CASE
            WHEN t.order_id IS NULL THEN 'missing_order_id'
            WHEN t.order_ts IS NULL THEN 'unparseable_order_ts'
            WHEN t.status IS NULL
                OR t.status NOT IN ('placed', 'shipped', 'delivered', 'cancelled')
                THEN 'invalid_status'
            WHEN c.customer_id IS NULL THEN 'orphan_customer_id'
        END AS invalid_reason
    FROM typed AS t
    LEFT JOIN (SELECT DISTINCT customer_id FROM silver.customers) AS c
        ON t.customer_id = c.customer_id
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY order_id, invalid_reason IS NULL
            ORDER BY updated_at DESC NULLS LAST, _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    _batch_id > strftime(order_ts, '%Y-%m') AS is_late_arriving,
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
