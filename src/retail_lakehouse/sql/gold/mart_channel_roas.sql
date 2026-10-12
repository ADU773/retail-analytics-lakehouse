-- description: Monthly channel performance - spend, clicks, last-touch attributed orders/revenue/margin, ROAS, POAS (margin / spend), CAC and CPC per traffic source.
-- depends_on: gold.fact_orders, silver.marketing_spend
-- materialized: table

WITH spend AS (
    SELECT
        CAST(date_trunc('month', spend_date) AS DATE) AS "month",
        channel,
        sum(spend)       AS spend,
        sum(clicks)      AS clicks,
        sum(impressions) AS impressions
    FROM silver.marketing_spend
    GROUP BY ALL
),

attributed AS (
    SELECT
        CAST(date_trunc('month', order_date) AS DATE) AS "month",
        attributed_channel                            AS channel,
        count(*)                                      AS orders,
        sum(net_revenue)                              AS attributed_revenue,
        sum(gross_margin)                             AS attributed_margin,
        count(*) FILTER (WHERE is_first_order)        AS new_customers
    FROM gold.fact_orders
    WHERE NOT is_cancelled
    GROUP BY ALL
)

SELECT
    coalesce(s."month", a."month")                               AS "month",
    coalesce(s.channel, a.channel)                               AS channel,
    CASE WHEN s.channel IS NOT NULL THEN 'paid' ELSE 'organic_owned' END AS channel_type,
    CAST(coalesce(s.spend, 0) AS DECIMAL(14, 2))                 AS spend,
    coalesce(s.clicks, 0)                                        AS clicks,
    coalesce(s.impressions, 0)                                   AS impressions,
    coalesce(a.orders, 0)                                        AS orders,
    CAST(coalesce(a.attributed_revenue, 0) AS DECIMAL(14, 2))    AS attributed_revenue,
    CAST(coalesce(a.attributed_margin, 0) AS DECIMAL(14, 2))     AS attributed_margin,
    coalesce(a.new_customers, 0)                                 AS new_customers,
    round(a.attributed_revenue / nullif(s.spend, 0), 3)          AS roas,
    round(a.attributed_margin / nullif(s.spend, 0), 3)           AS poas,
    round(s.spend / nullif(a.new_customers, 0), 2)               AS cac,
    round(s.spend / nullif(s.clicks, 0), 3)                      AS cpc
FROM spend AS s
FULL OUTER JOIN attributed AS a
    ON s."month" = a."month" AND s.channel = a.channel
ORDER BY 1, 2
