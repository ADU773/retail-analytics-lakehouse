-- description: Session fact (grain: one row per web session) with the deepest funnel step reached, event counts, conversion flag and the linked order.
-- depends_on: silver.web_sessions, silver.web_events, silver.customers, gold.dim_customer
-- materialized: table

WITH ev AS (
    SELECT
        session_id,
        count(*)                                           AS event_count,
        count(*) FILTER (WHERE event_type = 'product_view') AS product_views,
        max(CASE event_type
                WHEN 'purchase' THEN 4
                WHEN 'checkout' THEN 3
                WHEN 'add_to_cart' THEN 2
                WHEN 'product_view' THEN 1
            END)                                           AS funnel_step,
        max(order_id)                                      AS order_id,
        max(event_ts)                                      AS last_event_ts
    FROM silver.web_events
    GROUP BY session_id
),

country_region AS (
    SELECT country, mode(region) AS region
    FROM silver.customers
    GROUP BY country
)

SELECT
    s.session_id,
    CAST(strftime(s.session_start, '%Y%m%d') AS INTEGER)   AS session_date_key,
    s.session_start,
    CAST(s.session_start AS DATE)                          AS session_date,
    s.visitor_id,
    s.customer_id,
    CASE WHEN s.customer_id IS NULL THEN NULL
         ELSE coalesce(dc.customer_key, -1) END             AS customer_key,
    s.customer_id IS NOT NULL                              AS is_logged_in,
    s.device,
    s.traffic_source,
    s.country,
    coalesce(cr.region, 'Unknown')                         AS region,
    coalesce(ev.event_count, 0)                            AS event_count,
    coalesce(ev.product_views, 0)                          AS product_views,
    coalesce(ev.funnel_step, 0)                            AS funnel_step,
    coalesce(ev.funnel_step, 0) >= 1                       AS reached_product_view,
    coalesce(ev.funnel_step, 0) >= 2                       AS reached_add_to_cart,
    coalesce(ev.funnel_step, 0) >= 3                       AS reached_checkout,
    coalesce(ev.funnel_step, 0) >= 4                       AS reached_purchase,
    ev.order_id,
    greatest(date_diff('second', s.session_start, coalesce(ev.last_event_ts, s.session_start)), 0)
                                                           AS duration_seconds
FROM silver.web_sessions AS s
LEFT JOIN ev USING (session_id)
LEFT JOIN country_region AS cr USING (country)
LEFT JOIN gold.dim_customer AS dc
    ON dc.customer_id = s.customer_id
   AND s.session_start >= dc.valid_from
   AND s.session_start < dc.valid_to
