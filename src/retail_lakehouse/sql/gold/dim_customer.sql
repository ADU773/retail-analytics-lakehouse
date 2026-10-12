-- description: Customer dimension, SCD Type 2 on segment and location (region/country/city); e-mail, names and opt-in are Type 1 (latest value). Includes an unknown member (customer_key = -1).
-- depends_on: silver.customers
-- materialized: table

WITH versions AS (
    SELECT
        customer_id,
        updated_at,
        segment,
        region,
        country,
        city,
        lag(segment) OVER w AS prev_segment,
        lag(region) OVER w  AS prev_region,
        lag(country) OVER w AS prev_country,
        lag(city) OVER w    AS prev_city
    FROM silver.customers
    WINDOW w AS (PARTITION BY customer_id ORDER BY updated_at)
),

-- keep the first version and every version where a Type 2 attribute changed
changes AS (
    SELECT *
    FROM versions
    WHERE segment IS DISTINCT FROM prev_segment
       OR region IS DISTINCT FROM prev_region
       OR country IS DISTINCT FROM prev_country
       OR city IS DISTINCT FROM prev_city
),

scd AS (
    SELECT
        customer_id,
        segment,
        region,
        country,
        city,
        updated_at AS changed_at,
        row_number() OVER w AS version,
        CASE WHEN row_number() OVER w = 1 THEN TIMESTAMP '1900-01-01'
             ELSE updated_at END AS valid_from,
        coalesce(lead(updated_at) OVER w, TIMESTAMP '9999-12-31') AS valid_to,
        lead(updated_at) OVER w IS NULL AS is_current
    FROM changes
    WINDOW w AS (PARTITION BY customer_id ORDER BY updated_at)
),

-- Type 1 attributes: most recent value wins
latest AS (
    SELECT
        customer_id,
        arg_max(first_name, updated_at)       AS first_name,
        arg_max(last_name, updated_at)        AS last_name,
        arg_max(email, updated_at)            AS email,
        arg_max(is_email_valid, updated_at)   AS is_email_valid,
        arg_max(marketing_opt_in, updated_at) AS marketing_opt_in,
        arg_min(acquisition_channel, updated_at) AS acquisition_channel,
        min(signup_ts)                        AS signup_ts
    FROM silver.customers
    GROUP BY customer_id
)

SELECT
    row_number() OVER (ORDER BY s.customer_id, s.version) AS customer_key,
    s.customer_id,
    l.first_name,
    l.last_name,
    l.email,
    l.is_email_valid,
    l.marketing_opt_in,
    l.acquisition_channel,
    l.signup_ts,
    CAST(l.signup_ts AS DATE) AS signup_date,
    s.segment,
    s.region,
    s.country,
    s.city,
    s.version,
    s.changed_at,
    s.valid_from,
    s.valid_to,
    s.is_current
FROM scd AS s
JOIN latest AS l USING (customer_id)

UNION ALL

SELECT
    -1, 'UNKNOWN', 'Unknown', 'Unknown', NULL, FALSE, FALSE, 'unknown',
    NULL, NULL, 'unknown', 'Unknown', 'Unknown', 'Unknown', 1, NULL,
    TIMESTAMP '1900-01-01', TIMESTAMP '9999-12-31', TRUE
