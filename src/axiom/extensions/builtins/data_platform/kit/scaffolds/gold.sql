-- gold_{tenant_slug}.{name}: one SELECT over your silver, run as your tenant
-- role, so it only ever sees your own rows.
SELECT channel,
       unit,
       date_trunc('hour', ts) AS hour,
       avg(value)             AS mean,
       count(value)           AS readings
FROM silver.signals
GROUP BY channel, unit, date_trunc('hour', ts)
