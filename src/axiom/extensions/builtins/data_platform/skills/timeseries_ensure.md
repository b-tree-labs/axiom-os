# data.timeseries_ensure

Create the time partitions that should exist, ahead of the data.

A partition created only once its data arrives is too late: those rows already
fell into `DEFAULT`, where retention cannot reach them. This creates the current
period plus `ahead` future ones.

**Dry-run unless `apply` is passed.**

```
axi data timeseries_ensure                          # show what it would create
axi data timeseries_ensure --apply
axi data timeseries_ensure --table public.measurements --apply
```

## It refuses when DEFAULT holds rows and is unguarded

Adding a partition to a parent whose `DEFAULT` already has rows makes Postgres
scan `DEFAULT` under `ACCESS EXCLUSIVE` to prove no row belongs in the new
range. On a large default partition that stops every writer for the length of a
full-table scan. The refusal is the feature.

To proceed on a populated table, guard `DEFAULT` first with a validated upper
bound on the partition key — `ADD CONSTRAINT … CHECK (ts < boundary) NOT VALID`
(instant), then `VALIDATE CONSTRAINT` (a scan, but under `SHARE UPDATE
EXCLUSIVE`, so writers keep running). Postgres then skips the scan. The boundary
must be in the future, so rows still arriving satisfy it.
