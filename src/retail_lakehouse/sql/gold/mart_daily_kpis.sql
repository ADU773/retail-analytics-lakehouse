-- description: Daily executive KPIs - net revenue, orders, AOV, units, gross margin %, return rate, sessions, conversion rate, new customers, marketing spend and MER.
-- depends_on: gold.dim_date, gold.fact_orders, gold.fact_sessions, silver.marketing_spend
-- materialized: table

WITH o AS (
    SELECT
        order_date,
        count(*)                                  AS orders,
        sum(net_revenue)                          AS net_revenue,
        sum(gross_sales)                          AS gross_sales,
        sum(discount_amount)                      AS discounts,
        sum(units)                                AS units,
        sum(gross_margin)                         AS gross_margin,
        sum(returned_units)                       AS returned_units,
        sum(refund_amount)                        AS refunds,
        count(*) FILTER (WHERE is_first_order)    AS new_customers,
        count(DISTINCT customer_id)               AS active_customers
    FROM gold.fact_orders
    WHERE NOT is_cancelled
    GROUP BY order_date
),

s AS (
    SELECT
        session_date,
        count(*)                                     AS sessions,
        count(*) FILTER (WHERE reached_purchase)     AS converted_sessions
    FROM gold.fact_sessions
    GROUP BY session_date
),

m AS (
    SELECT spend_date, sum(spend) AS marketing_spend
    FROM silver.marketing_spend
    GROUP BY spend_date
)

SELECT
    d."date",
    d.date_key,
    d."year",
    d."month",
    d.year_month,
    d.day_name,
    d.day_of_week,
    d.is_weekend,
    d.holiday_name,
    d.is_peak_season,
    coalesce(o.orders, 0)                                         AS orders,
    CAST(coalesce(o.net_revenue, 0) AS DECIMAL(14, 2))            AS net_revenue,
    CAST(coalesce(o.gross_sales, 0) AS DECIMAL(14, 2))            AS gross_sales,
    CAST(coalesce(o.discounts, 0) AS DECIMAL(14, 2))              AS discounts,
    round(o.net_revenue / nullif(o.orders, 0), 2)                 AS aov,
    coalesce(o.units, 0)                                          AS units,
    CAST(coalesce(o.gross_margin, 0) AS DECIMAL(14, 2))           AS gross_margin,
    round(o.gross_margin / nullif(o.net_revenue, 0), 4)           AS gross_margin_pct,
    coalesce(o.returned_units, 0)                                 AS returned_units,
    round(o.returned_units / nullif(o.units, 0), 4)               AS return_rate,
    CAST(coalesce(o.refunds, 0) AS DECIMAL(14, 2))                AS refunds,
    coalesce(s.sessions, 0)                                       AS sessions,
    coalesce(s.converted_sessions, 0)                             AS converted_sessions,
    round(s.converted_sessions / nullif(s.sessions, 0), 4)        AS conversion_rate,
    coalesce(o.new_customers, 0)                                  AS new_customers,
    coalesce(o.active_customers, 0)                               AS active_customers,
    CAST(coalesce(m.marketing_spend, 0) AS DECIMAL(14, 2))        AS marketing_spend,
    round(o.net_revenue / nullif(m.marketing_spend, 0), 2)        AS mer
FROM gold.dim_date AS d
LEFT JOIN o ON o.order_date = d."date"
LEFT JOIN s ON s.session_date = d."date"
LEFT JOIN m ON m.spend_date = d."date"
WHERE d."date" >= DATE '{{ start_date }}' AND d."date" <= DATE '{{ as_of }}'
ORDER BY d."date"
