-- description: Market-basket analysis of product pairs bought together - support, confidence (both directions) and lift; pairs below a minimum co-occurrence count are dropped.
-- depends_on: gold.fact_order_items, gold.dim_product
-- materialized: table

WITH baskets AS (
    SELECT DISTINCT order_id, product_id
    FROM gold.fact_order_items
    WHERE NOT is_cancelled
),

totals AS (
    SELECT
        count(DISTINCT order_id) AS n_orders,
        greatest(3, CAST(ceil(count(DISTINCT order_id) * 0.0002) AS INTEGER)) AS min_pair_orders
    FROM baskets
),

item AS (
    SELECT product_id, count(*) AS orders_with_item
    FROM baskets
    GROUP BY product_id
),

pairs AS (
    SELECT a.product_id AS product_a, b.product_id AS product_b, count(*) AS orders_with_pair
    FROM baskets AS a
    JOIN baskets AS b ON a.order_id = b.order_id AND a.product_id < b.product_id
    GROUP BY ALL
)

SELECT
    p.product_a,
    pa.product_name                                         AS product_a_name,
    pa.subcategory                                          AS product_a_subcategory,
    p.product_b,
    pb.product_name                                         AS product_b_name,
    pb.subcategory                                          AS product_b_subcategory,
    p.orders_with_pair,
    ia.orders_with_item                                     AS orders_with_a,
    ib.orders_with_item                                     AS orders_with_b,
    round(p.orders_with_pair / t.n_orders, 6)               AS support,
    round(p.orders_with_pair / ia.orders_with_item, 4)      AS confidence_a_to_b,
    round(p.orders_with_pair / ib.orders_with_item, 4)      AS confidence_b_to_a,
    round(
        (p.orders_with_pair / t.n_orders)
        / ((ia.orders_with_item / t.n_orders) * (ib.orders_with_item / t.n_orders)),
        3
    )                                                       AS lift
FROM pairs AS p
CROSS JOIN totals AS t
JOIN item AS ia ON ia.product_id = p.product_a
JOIN item AS ib ON ib.product_id = p.product_b
JOIN gold.dim_product AS pa ON pa.product_id = p.product_a
JOIN gold.dim_product AS pb ON pb.product_id = p.product_b
WHERE p.orders_with_pair >= t.min_pair_orders
ORDER BY lift DESC, orders_with_pair DESC
