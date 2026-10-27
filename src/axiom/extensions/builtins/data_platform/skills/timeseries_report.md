# data.timeseries_report

Say where a partitioned time-series table's rows **actually are**, which is not
what the schema implies.

A `RANGE (ts)` parent with no time partitions is partitioned only in principle:
every row lands in `DEFAULT`, nothing fails, and nothing warns. This reports the
partitions that exist, the rows estimated in each, how many sit in `DEFAULT`,
which partitions are missing, and which are retirable.

Row counts are planner estimates (`pg_class.reltuples`), never `count(*)` —
counting hundreds of millions of rows to produce a health report is how a health
report becomes the thing that times out.

```
axi data timeseries_report                      # every declared table
axi data timeseries_report --table public.measurements
```

Reads only. Reports nothing when the node declares no tables, which is the
default.
