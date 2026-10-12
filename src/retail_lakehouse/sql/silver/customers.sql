-- description: Customer records (sign-ups and CDC change snapshots), typed and standardised. One row per customer_id + updated_at version; SCD2 history is built from these versions in gold.dim_customer.
-- depends_on: bronze.customers
-- materialized: table
-- quarantine: quarantine.customers

WITH typed AS (
    SELECT
        upper(nullif(trim(customer_id), ''))     AS customer_id,
        title_case(first_name)                   AS first_name,
        title_case(last_name)                    AS last_name,
        clean_email(email)                       AS email,
        clean_email(email) IS NOT NULL           AS is_email_valid,
        parse_ts(signup_ts)                      AS signup_ts,
        clean_code(acquisition_channel)          AS acquisition_channel,
        title_case(region)                       AS region,
        title_case(country)                      AS country,
        title_case(city)                         AS city,
        clean_code(segment)                      AS segment,
        coalesce(to_bool(marketing_opt_in), false) AS marketing_opt_in,
        parse_ts(updated_at)                     AS updated_at,
        email                                    AS raw_email,
        signup_ts                                AS raw_signup_ts,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.customers
),

validated AS (
    SELECT
        *,
        CASE
            WHEN customer_id IS NULL THEN 'missing_customer_id'
            WHEN signup_ts IS NULL THEN 'unparseable_signup_ts'
            WHEN updated_at IS NULL THEN 'unparseable_updated_at'
            WHEN segment IS NULL OR segment NOT IN ('standard', 'plus', 'business')
                THEN 'invalid_segment'
            WHEN region IS NULL THEN 'missing_region'
        END AS invalid_reason
    FROM typed
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY customer_id, updated_at, invalid_reason IS NULL
            ORDER BY _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
