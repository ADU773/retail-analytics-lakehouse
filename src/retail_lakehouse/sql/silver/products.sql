-- description: Product catalog with a conformed category hierarchy (one row per product_id).
-- depends_on: bronze.products
-- materialized: table
-- quarantine: quarantine.products

WITH typed AS (
    SELECT
        upper(nullif(trim(product_id), '')) AS product_id,
        trim(product_name)                  AS product_name,
        title_case(category)                AS category,
        title_case(subcategory)             AS subcategory,
        trim(brand)                         AS brand,
        to_money(list_price)                AS list_price,
        to_money(unit_cost)                 AS unit_cost,
        parse_ts(launched_at)               AS launched_at,
        list_price                          AS raw_list_price,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.products
),

validated AS (
    SELECT
        *,
        CASE
            WHEN product_id IS NULL THEN 'missing_product_id'
            WHEN list_price IS NULL OR list_price <= 0 THEN 'invalid_list_price'
            WHEN unit_cost IS NULL OR unit_cost < 0 THEN 'invalid_unit_cost'
            WHEN category IS NULL THEN 'missing_category'
        END AS invalid_reason
    FROM typed
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY product_id, invalid_reason IS NULL
            ORDER BY _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
