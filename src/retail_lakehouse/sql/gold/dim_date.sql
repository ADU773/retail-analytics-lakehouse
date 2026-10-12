-- description: Calendar dimension covering all activity through the as-of date, with retail events (Black Friday, Cyber Monday, holidays).
-- depends_on: silver.orders, silver.web_sessions, silver.marketing_spend
-- materialized: table

WITH bounds AS (
    SELECT
        least(
            (SELECT min(order_ts) FROM silver.orders)::DATE,
            (SELECT min(session_start) FROM silver.web_sessions)::DATE,
            (SELECT min(spend_date) FROM silver.marketing_spend)
        ) AS lo,
        greatest(
            (SELECT max(order_ts) FROM silver.orders)::DATE,
            DATE '{{ as_of }}'
        ) AS hi
),

days AS (
    SELECT CAST(unnest(generate_series(lo::TIMESTAMP, hi::TIMESTAMP, INTERVAL 1 DAY)) AS DATE) AS d
    FROM bounds
),

events AS (
    SELECT
        d,
        make_date(year(d), 11, 1) AS nov1
    FROM days
),

retail AS (
    SELECT
        d,
        -- Thanksgiving = 4th Thursday of November; Black Friday = the day after.
        nov1 + CAST(((4 - isodow(nov1) + 7) % 7) + 21 AS INTEGER) AS thanksgiving
    FROM events
)

SELECT
    CAST(strftime(r.d, '%Y%m%d') AS INTEGER)             AS date_key,
    r.d                                                  AS "date",
    year(r.d)                                            AS "year",
    quarter(r.d)                                         AS quarter,
    month(r.d)                                           AS "month",
    monthname(r.d)                                       AS month_name,
    strftime(r.d, '%Y-%m')                               AS year_month,
    CAST(date_trunc('month', r.d) AS DATE)               AS month_start,
    weekofyear(r.d)                                      AS iso_week,
    isodow(r.d)                                          AS day_of_week,
    dayname(r.d)                                         AS day_name,
    isodow(r.d) >= 6                                     AS is_weekend,
    r.d = r.thanksgiving + 1                             AS is_black_friday,
    r.d = r.thanksgiving + 4                             AS is_cyber_monday,
    CASE
        WHEN r.d = r.thanksgiving + 1 THEN 'Black Friday'
        WHEN r.d = r.thanksgiving + 4 THEN 'Cyber Monday'
        WHEN r.d BETWEEN r.thanksgiving + 2 AND r.thanksgiving + 3 THEN 'Black Friday Weekend'
        WHEN r.d = r.thanksgiving THEN 'Thanksgiving'
        WHEN month(r.d) = 12 AND day(r.d) = 25 THEN 'Christmas Day'
        WHEN month(r.d) = 12 AND day(r.d) = 26 THEN 'Boxing Day'
        WHEN month(r.d) = 1 AND day(r.d) = 1 THEN 'New Year''s Day'
    END                                                  AS holiday_name,
    (month(r.d) = 11 AND r.d >= r.thanksgiving - 3) OR month(r.d) = 12 AS is_peak_season
FROM retail AS r
ORDER BY r.d
