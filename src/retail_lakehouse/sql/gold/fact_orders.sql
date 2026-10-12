-- description: Order fact (grain: one row per order) with point-in-time customer attributes, last-touch attribution from the purchase session, revenue, margin and returns.
-- depends_on: silver.orders, gold.fact_order_items, gold.dim_customer, silver.web_events, silver.web_sessions
-- materialized: table

WITH lines AS (
    SELECT
        order_id,
        count(*)                AS line_count,
        sum(quantity)           AS units,
        sum(gross_amount)       AS gross_sales,
        sum(discount_amount)    AS discount_amount,
        sum(net_amount)         AS net_revenue,
        sum(cogs)               AS cogs,
        sum(gross_margin)       AS gross_margin,
        sum(returned_quantity)  AS returned_units,
        sum(refund_amount)      AS refund_amount
    FROM gold.fact_order_items
    GROUP BY order_id
),

-- Last-touch attribution: the session that contains the purchase event.
purchase_session AS (
    SELECT
        e.order_id,
        arg_min(s.session_id, e.event_ts)     AS session_id,
        arg_min(s.traffic_source, e.event_ts) AS attributed_channel,
        arg_min(s.device, e.event_ts)         AS device
    FROM silver.web_events AS e
    JOIN silver.web_sessions AS s USING (session_id)
    WHERE e.event_type = 'purchase' AND e.order_id IS NOT NULL
    GROUP BY e.order_id
),

sequenced AS (
    SELECT
        order_id,
        row_number() OVER (PARTITION BY customer_id ORDER BY order_ts, order_id) AS order_sequence
    FROM silver.orders
    WHERE status <> 'cancelled'
)

SELECT
    o.order_id,
    CAST(strftime(o.order_ts, '%Y%m%d') AS INTEGER)       AS order_date_key,
    o.order_ts,
    CAST(o.order_ts AS DATE)                              AS order_date,
    coalesce(dc.customer_key, -1)                         AS customer_key,
    o.customer_id,
    coalesce(dc.segment, 'unknown')                       AS customer_segment,
    coalesce(dc.region, 'Unknown')                        AS customer_region,
    coalesce(dc.country, 'Unknown')                       AS customer_country,
    o.status,
    o.status = 'cancelled'                                AS is_cancelled,
    o.payment_method,
    o.discount_code,
    CASE
        WHEN o.discount_code IS NULL THEN 'none'
        WHEN o.discount_code = 'WELCOME10' THEN 'welcome'
        WHEN o.discount_code LIKE 'BFCM%' THEN 'black_friday'
        ELSE 'seasonal'
    END                                                   AS promotion_type,
    coalesce(l.line_count, 0)                             AS line_count,
    coalesce(l.units, 0)                                  AS units,
    coalesce(l.gross_sales, 0)                            AS gross_sales,
    coalesce(l.discount_amount, 0)                        AS discount_amount,
    coalesce(l.net_revenue, 0)                            AS net_revenue,
    coalesce(l.cogs, 0)                                   AS cogs,
    coalesce(l.gross_margin, 0)                           AS gross_margin,
    o.shipping_fee,
    o.order_total,
    coalesce(l.returned_units, 0)                         AS returned_units,
    coalesce(l.refund_amount, 0)                          AS refund_amount,
    coalesce(l.returned_units, 0) > 0                     AS has_return,
    ps.session_id,
    coalesce(ps.attributed_channel, 'unattributed')       AS attributed_channel,
    coalesce(ps.device, 'unknown')                        AS device,
    sq.order_sequence,
    coalesce(sq.order_sequence = 1, FALSE)                AS is_first_order,
    o.is_late_arriving,
    o.order_ts_format
FROM silver.orders AS o
LEFT JOIN lines AS l USING (order_id)
LEFT JOIN purchase_session AS ps USING (order_id)
LEFT JOIN sequenced AS sq USING (order_id)
LEFT JOIN gold.dim_customer AS dc
    ON dc.customer_id = o.customer_id
   AND o.order_ts >= dc.valid_from
   AND o.order_ts < dc.valid_to
