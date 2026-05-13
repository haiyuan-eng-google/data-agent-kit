-- ============================================================
-- UCP Analytics Dashboard Queries
-- For use in Looker Studio, BigQuery console, or Metabase
-- ============================================================

-- 1. CHECKOUT FUNNEL (daily conversion rates)
SELECT
    DATE(timestamp) AS day,
    COUNT(CASE WHEN event_type = 'checkout_session_created'   THEN 1 END) AS started,
    COUNT(CASE WHEN event_type = 'checkout_session_updated'   THEN 1 END) AS updated,
    COUNT(CASE WHEN event_type = 'checkout_session_completed' THEN 1 END) AS completed,
    COUNT(CASE WHEN event_type = 'checkout_session_canceled'  THEN 1 END) AS canceled,
    COUNT(CASE WHEN event_type = 'checkout_escalation'        THEN 1 END) AS escalated,
    SAFE_DIVIDE(
        COUNT(CASE WHEN event_type = 'checkout_session_completed' THEN 1 END),
        COUNT(CASE WHEN event_type = 'checkout_session_created'   THEN 1 END)
    ) AS conversion_rate
FROM `PROJECT.ucp_analytics.ucp_events`
GROUP BY day
ORDER BY day DESC;


-- 2. REVENUE BY MERCHANT (daily)
SELECT
    DATE(timestamp) AS day,
    merchant_host,
    COUNT(DISTINCT checkout_session_id) AS transactions,
    SUM(total_amount) / 100.0 AS revenue_dollars,
    AVG(total_amount) / 100.0 AS avg_order_value,
    currency
FROM `PROJECT.ucp_analytics.ucp_events`
WHERE event_type = 'checkout_session_completed'
GROUP BY day, merchant_host, currency
ORDER BY revenue_dollars DESC;


-- 3. PAYMENT HANDLER MIX
SELECT
    payment_handler_id,
    payment_brand,
    COUNT(*) AS transactions,
    SUM(total_amount) / 100.0 AS revenue_dollars,
    AVG(latency_ms) AS avg_latency_ms
FROM `PROJECT.ucp_analytics.ucp_events`
WHERE event_type = 'checkout_session_completed'
  AND payment_handler_id IS NOT NULL
GROUP BY payment_handler_id, payment_brand
ORDER BY transactions DESC;


-- 4. CAPABILITY ADOPTION ACROSS MERCHANTS
SELECT
    JSON_VALUE(cap, '$.name') AS capability_name,
    JSON_VALUE(cap, '$.version') AS capability_version,
    CASE WHEN JSON_VALUE(cap, '$.extends') IS NOT NULL
         THEN 'extension' ELSE 'core' END AS cap_type,
    COUNT(DISTINCT merchant_host) AS merchant_count,
    COUNT(DISTINCT checkout_session_id) AS session_count
FROM `PROJECT.ucp_analytics.ucp_events`,
    UNNEST(JSON_QUERY_ARRAY(capabilities_json)) AS cap
WHERE capabilities_json IS NOT NULL
GROUP BY capability_name, capability_version, cap_type
ORDER BY session_count DESC;


-- 5. ERROR ANALYSIS
SELECT
    error_code,
    error_severity,
    error_message,
    COUNT(*) AS occurrences,
    COUNT(DISTINCT checkout_session_id) AS affected_sessions,
    COUNT(DISTINCT merchant_host) AS affected_merchants
FROM `PROJECT.ucp_analytics.ucp_events`
WHERE error_code IS NOT NULL
GROUP BY error_code, error_severity, error_message
ORDER BY occurrences DESC;


-- 6. CHECKOUT ESCALATION RATE (human handoff)
SELECT
    merchant_host,
    COUNT(CASE WHEN event_type = 'checkout_session_created' THEN 1 END) AS total_sessions,
    COUNT(CASE WHEN event_type = 'checkout_escalation'      THEN 1 END) AS escalations,
    SAFE_DIVIDE(
        COUNT(CASE WHEN event_type = 'checkout_escalation'      THEN 1 END),
        COUNT(CASE WHEN event_type = 'checkout_session_created' THEN 1 END)
    ) AS escalation_rate
FROM `PROJECT.ucp_analytics.ucp_events`
GROUP BY merchant_host
HAVING total_sessions > 0
ORDER BY escalation_rate DESC;


-- 7. LATENCY PERCENTILES BY OPERATION
SELECT
    event_type,
    COUNT(*) AS calls,
    APPROX_QUANTILES(latency_ms, 100)[OFFSET(50)] AS p50_ms,
    APPROX_QUANTILES(latency_ms, 100)[OFFSET(95)] AS p95_ms,
    APPROX_QUANTILES(latency_ms, 100)[OFFSET(99)] AS p99_ms,
    MAX(latency_ms) AS max_ms
FROM `PROJECT.ucp_analytics.ucp_events`
WHERE latency_ms IS NOT NULL
GROUP BY event_type
ORDER BY p95_ms DESC;


-- 8. FULFILLMENT GEOGRAPHY
SELECT
    fulfillment_destination_country,
    fulfillment_type,
    COUNT(*) AS orders,
    SUM(total_amount) / 100.0 AS revenue_dollars,
    SUM(fulfillment_amount) / 100.0 AS total_fulfillment
FROM `PROJECT.ucp_analytics.ucp_events`
WHERE event_type = 'checkout_session_completed'
  AND fulfillment_destination_country IS NOT NULL
GROUP BY fulfillment_destination_country, fulfillment_type
ORDER BY orders DESC;


-- 9. SESSION TIMELINE (debug a specific checkout)
SELECT
    timestamp,
    event_type,
    checkout_status,
    http_method,
    http_path,
    http_status_code,
    total_amount,
    error_code,
    error_message,
    latency_ms
FROM `PROJECT.ucp_analytics.ucp_events`
WHERE checkout_session_id = 'SESSION_ID_HERE'
ORDER BY timestamp;


-- 10. DISCOVERY HIT RATE (how many profiles fetch → checkout)
WITH discovery AS (
    SELECT
        merchant_host,
        DATE(timestamp) AS day,
        COUNT(*) AS profile_fetches
    FROM `PROJECT.ucp_analytics.ucp_events`
    WHERE event_type = 'profile_discovered'
    GROUP BY merchant_host, day
),
checkouts AS (
    SELECT
        merchant_host,
        DATE(timestamp) AS day,
        COUNT(*) AS checkout_starts
    FROM `PROJECT.ucp_analytics.ucp_events`
    WHERE event_type = 'checkout_session_created'
    GROUP BY merchant_host, day
)
SELECT
    d.merchant_host,
    d.day,
    d.profile_fetches,
    COALESCE(c.checkout_starts, 0) AS checkout_starts,
    SAFE_DIVIDE(c.checkout_starts, d.profile_fetches) AS discovery_to_checkout_rate
FROM discovery d
LEFT JOIN checkouts c USING (merchant_host, day)
ORDER BY d.day DESC;
