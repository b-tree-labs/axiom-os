-- A gold object: one SELECT over your silver. It becomes the view
-- gold_{{tenant_slug}}.daily_stats, and it runs as your tenant role, so it only
-- ever sees your own rows: no `WHERE site = ...` needed, and none would widen it.
--
-- The window: the last {{example_window_days}} days, written by kit-init to reach the bundled
-- samples. A view over the site's whole history runs past the node's statement timeout, so keep
-- a bound on ts; when your own data arrives, shorten it (say '7 days').
SELECT channel,
       unit,
       date_trunc('day', ts) AS day,
       avg(value)            AS mean,
       min(value)            AS low,
       max(value)            AS high,
       count(*)              AS readings
FROM silver.signals
WHERE ts >= now() - interval '{{example_window_days}} days'
GROUP BY channel, unit, date_trunc('day', ts)
