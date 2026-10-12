-- description: Daily paid-media spend per channel (one row per spend_date + channel); negative refund adjustments are quarantined.
-- depends_on: bronze.marketing_spend
-- materialized: table
-- quarantine: quarantine.marketing_spend

WITH typed AS (
    SELECT
        try_cast(trim("date") AS DATE) AS spend_date,
        clean_code(channel)            AS channel,
        to_money(spend)                AS spend,
        to_int(impressions)            AS impressions,
        to_int(clicks)                 AS clicks,
        upper(trim(currency))          AS currency,
        spend                          AS raw_spend,
        _batch_id,
        _source_file,
        _ingested_at
    FROM bronze.marketing_spend
),

validated AS (
    SELECT
        *,
        CASE
            WHEN spend_date IS NULL THEN 'unparseable_date'
            WHEN channel IS NULL OR channel NOT IN (
                'paid_search', 'paid_social', 'display', 'affiliate', 'email'
            ) THEN 'invalid_channel'
            WHEN spend IS NULL THEN 'missing_spend'
            WHEN spend < 0 THEN 'negative_spend'
        END AS invalid_reason
    FROM typed
),

ranked AS (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY spend_date, channel, invalid_reason IS NULL
            ORDER BY _ingested_at DESC, _source_file DESC
        ) AS _rn
    FROM validated
)

SELECT
    * EXCLUDE (_rn, invalid_reason),
    coalesce(invalid_reason, CASE WHEN _rn > 1 THEN 'duplicate_record' END) AS rejection_reason
FROM ranked
