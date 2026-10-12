-- description: Product dimension with the category > subcategory hierarchy, brand, list price, unit cost and list margin.
-- depends_on: silver.products
-- materialized: table

SELECT
    row_number() OVER (ORDER BY product_id)               AS product_key,
    product_id,
    product_name,
    category,
    subcategory,
    brand,
    list_price,
    unit_cost,
    round(1 - unit_cost / list_price, 4)                  AS list_margin_pct,
    launched_at,
    CAST(launched_at AS DATE)                             AS launch_date
FROM silver.products
