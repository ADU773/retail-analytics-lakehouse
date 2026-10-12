-- description: Data-quality audit mart - rows rejected by the silver layer per entity and rejection reason, with the share of the entity's bronze rows.
-- depends_on: quarantine.customers, quarantine.products, quarantine.orders, quarantine.order_items, quarantine.returns, quarantine.web_sessions, quarantine.web_events, quarantine.marketing_spend
-- depends_on: bronze.customers, bronze.products, bronze.orders, bronze.order_items, bronze.returns, bronze.web_sessions, bronze.web_events, bronze.marketing_spend
-- materialized: table

WITH rejected AS (
    SELECT 'customers' AS entity, rejection_reason FROM quarantine.customers
    UNION ALL SELECT 'products', rejection_reason FROM quarantine.products
    UNION ALL SELECT 'orders', rejection_reason FROM quarantine.orders
    UNION ALL SELECT 'order_items', rejection_reason FROM quarantine.order_items
    UNION ALL SELECT 'returns', rejection_reason FROM quarantine.returns
    UNION ALL SELECT 'web_sessions', rejection_reason FROM quarantine.web_sessions
    UNION ALL SELECT 'web_events', rejection_reason FROM quarantine.web_events
    UNION ALL SELECT 'marketing_spend', rejection_reason FROM quarantine.marketing_spend
),

bronze_rows AS (
    SELECT 'customers' AS entity, count(*) AS bronze_rows FROM bronze.customers
    UNION ALL SELECT 'products', count(*) FROM bronze.products
    UNION ALL SELECT 'orders', count(*) FROM bronze.orders
    UNION ALL SELECT 'order_items', count(*) FROM bronze.order_items
    UNION ALL SELECT 'returns', count(*) FROM bronze.returns
    UNION ALL SELECT 'web_sessions', count(*) FROM bronze.web_sessions
    UNION ALL SELECT 'web_events', count(*) FROM bronze.web_events
    UNION ALL SELECT 'marketing_spend', count(*) FROM bronze.marketing_spend
)

SELECT
    r.entity,
    r.rejection_reason,
    count(*)                                  AS rejected_rows,
    b.bronze_rows,
    round(count(*) / b.bronze_rows, 5)        AS share_of_bronze
FROM rejected AS r
JOIN bronze_rows AS b USING (entity)
GROUP BY r.entity, r.rejection_reason, b.bronze_rows
ORDER BY r.entity, rejected_rows DESC
