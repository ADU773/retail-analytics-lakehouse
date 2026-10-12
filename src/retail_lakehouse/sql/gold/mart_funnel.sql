-- description: Monthly web funnel by device and traffic source - sessions reaching product_view, add_to_cart, checkout and purchase, with step and overall conversion rates.
-- depends_on: gold.fact_sessions
-- materialized: table

SELECT
    CAST(date_trunc('month', session_date) AS DATE)                          AS "month",
    device,
    traffic_source,
    region,
    count(*)                                                                 AS sessions,
    count(*) FILTER (WHERE reached_product_view)                             AS product_view_sessions,
    count(*) FILTER (WHERE reached_add_to_cart)                              AS add_to_cart_sessions,
    count(*) FILTER (WHERE reached_checkout)                                 AS checkout_sessions,
    count(*) FILTER (WHERE reached_purchase)                                 AS purchase_sessions,
    round(count(*) FILTER (WHERE reached_product_view) / count(*), 4)        AS view_rate,
    round(count(*) FILTER (WHERE reached_add_to_cart)
          / nullif(count(*) FILTER (WHERE reached_product_view), 0), 4)      AS cart_rate,
    round(count(*) FILTER (WHERE reached_checkout)
          / nullif(count(*) FILTER (WHERE reached_add_to_cart), 0), 4)       AS checkout_rate,
    round(count(*) FILTER (WHERE reached_purchase)
          / nullif(count(*) FILTER (WHERE reached_checkout), 0), 4)          AS purchase_rate,
    round(count(*) FILTER (WHERE reached_purchase) / count(*), 4)            AS conversion_rate
FROM gold.fact_sessions
GROUP BY ALL
ORDER BY 1, 2, 3, 4
