-- Shared SQL macros, installed into every DuckDB connection the pipeline opens.

-- Parse the timestamp variants found in the landing zone and normalise them to UTC:
--   ISO-8601 with Z or an explicit offset, US month/day format, Unix epoch seconds,
--   plain dates. Returns NULL when nothing matches (the caller decides what to do).
CREATE OR REPLACE MACRO parse_ts(s) AS (
    CASE
        WHEN s IS NULL OR trim(s) = '' THEN NULL
        WHEN regexp_full_match(trim(s), '\d{9,11}')
            THEN CAST(to_timestamp(CAST(trim(s) AS BIGINT)) AS TIMESTAMP)
        WHEN regexp_full_match(trim(s), '\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2}')
            THEN try_strptime(trim(s), '%m/%d/%Y %H:%M:%S')
        WHEN regexp_full_match(trim(s), '\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})')
            THEN CAST(try_cast(trim(s) AS TIMESTAMPTZ) AS TIMESTAMP)
        ELSE try_cast(trim(s) AS TIMESTAMP)
    END
);

-- Trim + collapse case for categorical codes such as ' PAID_SEARCH ' -> 'paid_search'.
CREATE OR REPLACE MACRO clean_code(s) AS nullif(lower(trim(s)), '');

-- Title-case free-text labels such as '  NORTH america ' -> 'North America'.
CREATE OR REPLACE MACRO title_case(s) AS nullif(
    array_to_string(
        list_transform(string_split(lower(trim(s)), ' '), lambda w: upper(w[1]) || w[2:]),
        ' '
    ),
    ''
);

-- Syntactically valid e-mail address (lower-cased) or NULL.
CREATE OR REPLACE MACRO clean_email(s) AS (
    CASE
        WHEN regexp_full_match(lower(trim(s)), '[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}')
            THEN lower(trim(s))
    END
);

-- Safe numeric casts.
CREATE OR REPLACE MACRO to_money(s) AS CAST(try_cast(trim(s) AS DOUBLE) AS DECIMAL(12, 2));
CREATE OR REPLACE MACRO to_int(s) AS try_cast(trim(s) AS INTEGER);
CREATE OR REPLACE MACRO to_bool(s) AS (
    CASE
        WHEN lower(trim(s)) IN ('true', 't', 'yes', 'y', '1') THEN TRUE
        WHEN lower(trim(s)) IN ('false', 'f', 'no', 'n', '0') THEN FALSE
    END
);
