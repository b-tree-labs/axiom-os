# data.timeseries_retire

Drop time partitions lying **entirely** before the retention window.

A `DROP` returns the space at once, indexes included, with no `VACUUM` and no
bloat. That is the reason to partition a time-series table: without partitions,
retention is a mass `DELETE` that bloats instead of returning space.

**Dry-run unless `apply` is passed.** Nothing is dropped unless the table
declares `retain_periods`.

```
axi data timeseries_retire                          # show what it would drop
axi data timeseries_retire --apply
```

## What it will never drop

A partition whose range overlaps the cutoff, because dropping it would discard
rows inside the window — a retention policy that silently deletes retained data
is worse than none. And the `DEFAULT` partition, whatever it holds: it has no
upper bound, so it can always contain rows inside the window.
