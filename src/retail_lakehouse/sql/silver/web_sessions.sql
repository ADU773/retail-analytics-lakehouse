-- description: Web sessions (anonymous and logged-in) with conformed device / traffic-source codes and UTC start times (one row per session_id).
-- depends_on: bronze.web_sessions, silver.customers
-- materialized: table
-- quarantine: quarantine.web_sessions

WITH typed AS (
    SELECT
        nullif(trim(session_id), '')          AS session_id,
        nullif(trim(visitor_id), '')          AS visitor_id,
        upper(nullif(trim(customer_id), ''))  AS customer_id,
        parse_ts(session_start)               AS session_start,
        clean_code(device)                    AS device,
        clean_code(traffic_source)            AS traffic_source,
        title_case(country)                   AS country,
        session_start                         AS raw_session_start,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.web_sessions
),

validated AS (
    SELECT
        t.*,
        CASE
            WHEN t.session_id IS NULL THEN 'missing_session_id'
            WHEN t.session_start IS NULL THEN 'unparseable_session_start'
            WHEN t.device IS NULL OR t.device NOT IN ('desktop', 'mobile', 'tablet')
                THEN 'invalid_device'
            WHEN t.traffic_source IS NULL OR t.traffic_source NOT IN (
                'organic_search', 'paid_search', 'paid_social', 'display', 'email',
                'affiliate', 'referral', 'direct'
            ) THEN 'invalid_traffic_source'
            WHEN t.customer_id IS NOT NULL AND c.customer_id IS NULL THEN 'orphan_customer_id'
        END AS invalid_reason
    FROM typed AS t
    LEFT JOIN (SELECT DISTINCT customer_id FROM silver.customers) AS c
        ON t.customer_id = c.customer_id
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY session_id, invalid_reason IS NULL
            ORDER BY _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
