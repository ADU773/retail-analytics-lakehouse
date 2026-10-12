-- description: Order-line fact (grain: one row per order line) with revenue, discount, COGS, margin and returns; customer_key is the SCD2 version valid at order time.
-- depends_on: silver.order_items, silver.orders, silver.returns, gold.dim_product, gold.dim_customer
-- materialized: table

WITH returns AS (
    SELECT
        order_item_id,
        sum(quantity)                     AS returned_quantity,
        sum(refund_amount)                AS refund_amount,
        arg_min(reason, requested_at)     AS return_reason,
        min(requested_at)                 AS first_return_at
    FROM silver.returns
    GROUP BY order_item_id
)

SELECT
    i.order_item_id,
    i.order_id,
    CAST(strftime(o.order_ts, '%Y%m%d') AS INTEGER)        AS order_date_key,
    o.order_ts,
    CAST(o.order_ts AS DATE)                               AS order_date,
    coalesce(dc.customer_key, -1)                          AS customer_key,
    o.customer_id,
    p.product_key,
    i.product_id,
    p.category,
    p.subcategory,
    p.brand,
    o.status = 'cancelled'                                 AS is_cancelled,
    i.quantity,
    i.unit_price,
    i.discount_amount,
    CAST(i.quantity * i.unit_price AS DECIMAL(14, 2))                     AS gross_amount,
    CAST(i.quantity * i.unit_price - i.discount_amount AS DECIMAL(14, 2)) AS net_amount,
    CAST(i.quantity * p.unit_cost AS DECIMAL(14, 2))                      AS cogs,
    CAST(i.quantity * i.unit_price - i.discount_amount - i.quantity * p.unit_cost
         AS DECIMAL(14, 2))                                               AS gross_margin,
    coalesce(r.returned_quantity, 0)                       AS returned_quantity,
    CAST(coalesce(r.refund_amount, 0) AS DECIMAL(14, 2))   AS refund_amount,
    r.return_reason,
    r.first_return_at,
    r.order_item_id IS NOT NULL                            AS is_returned
FROM silver.order_items AS i
JOIN silver.orders AS o USING (order_id)
JOIN gold.dim_product AS p USING (product_id)
LEFT JOIN gold.dim_customer AS dc
    ON dc.customer_id = o.customer_id
   AND o.order_ts >= dc.valid_from
   AND o.order_ts < dc.valid_to
LEFT JOIN returns AS r USING (order_item_id)
