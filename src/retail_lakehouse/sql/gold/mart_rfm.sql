-- description: RFM scoring per purchasing customer (quintile scores 1-5 as of the as-of date) mapped to named segments such as Champions, Loyal Customers, At Risk and Hibernating.
-- depends_on: gold.fact_orders, gold.dim_customer
-- materialized: table

WITH base AS (
    SELECT
        customer_id,
        min(order_date)   AS first_order_date,
        max(order_date)   AS last_order_date,
        count(*)          AS frequency,
        sum(net_revenue)  AS monetary
    FROM gold.fact_orders
    WHERE NOT is_cancelled AND customer_key <> -1
    GROUP BY customer_id
),

scored AS (
    SELECT
        *,
        date_diff('day', last_order_date, DATE '{{ as_of }}') AS recency_days,
        -- quintiles; ties are broken deterministically so every score holds ~20% of customers
        ntile(5) OVER (ORDER BY last_order_date, customer_id)          AS r_score,
        ntile(5) OVER (ORDER BY frequency, monetary, customer_id)      AS f_score,
        ntile(5) OVER (ORDER BY monetary, customer_id)                 AS m_score
    FROM base
)

SELECT
    s.customer_id,
    d.segment         AS customer_segment,
    d.region,
    d.acquisition_channel,
    s.first_order_date,
    s.last_order_date,
    s.recency_days,
    s.frequency,
    CAST(s.monetary AS DECIMAL(14, 2)) AS monetary,
    s.r_score,
    s.f_score,
    s.m_score,
    concat(s.r_score, s.f_score, s.m_score) AS rfm_cell,
    s.r_score + s.f_score + s.m_score       AS rfm_total,
    -- classic R x F segment map
    CASE
        WHEN s.r_score = 5 AND s.f_score >= 4 THEN 'Champions'
        WHEN s.r_score BETWEEN 3 AND 4 AND s.f_score >= 4 THEN 'Loyal Customers'
        WHEN s.r_score >= 4 AND s.f_score BETWEEN 2 AND 3 THEN 'Potential Loyalists'
        WHEN s.r_score = 5 AND s.f_score = 1 THEN 'New Customers'
        WHEN s.r_score = 4 AND s.f_score = 1 THEN 'Promising'
        WHEN s.r_score = 3 AND s.f_score = 3 THEN 'Need Attention'
        WHEN s.r_score = 3 AND s.f_score <= 2 THEN 'About to Sleep'
        WHEN s.r_score <= 2 AND s.f_score = 5 THEN 'Cannot Lose Them'
        WHEN s.r_score <= 2 AND s.f_score BETWEEN 3 AND 4 THEN 'At Risk'
        ELSE 'Hibernating'
    END AS rfm_segment
FROM scored AS s
LEFT JOIN gold.dim_customer AS d
    ON d.customer_id = s.customer_id AND d.is_current
