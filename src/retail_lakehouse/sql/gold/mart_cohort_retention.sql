-- description: Monthly acquisition cohorts (month of first order) x months since first order - active customers, retention rate and cumulative revenue per customer.
-- depends_on: gold.fact_orders
-- materialized: table

WITH valid AS (
    SELECT customer_id, order_date, net_revenue
    FROM gold.fact_orders
    WHERE NOT is_cancelled AND customer_key <> -1
),

first_order AS (
    SELECT customer_id, CAST(date_trunc('month', min(order_date)) AS DATE) AS cohort_month
    FROM valid
    GROUP BY customer_id
),

cohort_size AS (
    SELECT cohort_month, count(*) AS cohort_size
    FROM first_order
    GROUP BY cohort_month
),

activity AS (
    SELECT
        f.cohort_month,
        date_diff('month', f.cohort_month, CAST(date_trunc('month', v.order_date) AS DATE))
            AS months_since_first_order,
        v.customer_id,
        v.net_revenue
    FROM valid AS v
    JOIN first_order AS f USING (customer_id)
),

grid AS (
    SELECT
        cohort_month,
        months_since_first_order,
        count(DISTINCT customer_id) AS active_customers,
        sum(net_revenue)            AS revenue
    FROM activity
    GROUP BY ALL
)

SELECT
    g.cohort_month,
    strftime(g.cohort_month, '%Y-%m')                                  AS cohort,
    g.months_since_first_order,
    c.cohort_size,
    g.active_customers,
    round(g.active_customers / c.cohort_size, 4)                       AS retention_rate,
    CAST(g.revenue AS DECIMAL(14, 2))                                  AS revenue,
    round(sum(g.revenue) OVER (
        PARTITION BY g.cohort_month ORDER BY g.months_since_first_order
    ) / c.cohort_size, 2)                                              AS cumulative_revenue_per_customer,
    -- the most recent month of each cohort can be incomplete at the as-of date
    (g.cohort_month + to_months(g.months_since_first_order + 1)) <= DATE '{{ as_of }}' + 1
                                                                       AS is_complete_period
FROM grid AS g
JOIN cohort_size AS c USING (cohort_month)
ORDER BY g.cohort_month, g.months_since_first_order
