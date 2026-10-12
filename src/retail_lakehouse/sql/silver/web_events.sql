-- description: Funnel events (product_view, add_to_cart, checkout, purchase) normalised to UTC and linked to valid sessions (one row per event_id).
-- depends_on: bronze.web_events, silver.web_sessions
-- materialized: table
-- quarantine: quarantine.web_events

WITH typed AS (
    SELECT
        nullif(trim(event_id), '')            AS event_id,
        nullif(trim(session_id), '')          AS session_id,
        clean_code(event_type)                AS event_type,
        parse_ts(event_ts)                    AS event_ts,
        upper(nullif(trim(product_id), ''))   AS product_id,
        upper(nullif(trim(order_id), ''))     AS order_id,
        event_ts                              AS raw_event_ts,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.web_events
),

validated AS (
    SELECT
        t.*,
        CASE
            WHEN t.event_id IS NULL THEN 'missing_event_id'
            WHEN t.event_type IS NULL OR t.event_type NOT IN (
                'product_view', 'add_to_cart', 'checkout', 'purchase'
            ) THEN 'invalid_event_type'
            WHEN t.event_ts IS NULL THEN 'unparseable_event_ts'
            WHEN s.session_id IS NULL THEN 'orphan_session_id'
        END AS invalid_reason
    FROM typed AS t
    LEFT JOIN silver.web_sessions AS s ON t.session_id = s.session_id
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY event_id, invalid_reason IS NULL
            ORDER BY _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    _batch_id > strftime(event_ts, '%Y-%m') AS is_late_arriving,
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
