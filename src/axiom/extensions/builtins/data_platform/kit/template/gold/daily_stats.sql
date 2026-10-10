-- A gold object: one SELECT over your silver. It becomes the view
-- gold_{{tenant_slug}}.daily_stats, and it runs as your tenant role, so it only
-- ever sees your own rows: no `WHERE site = ...` needed, and none would widen it.
SELECT channel,
       unit,
       date_trunc('day', ts) AS day,
       avg(value)            AS mean,
       min(value)            AS low,
       max(value)            AS high,
       count(*)              AS readings
FROM silver.signals
GROUP BY channel, unit, date_trunc('day', ts)
