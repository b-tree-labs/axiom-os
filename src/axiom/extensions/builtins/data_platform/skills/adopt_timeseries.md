# data.adopt_timeseries

Adopt a populated plain table into the time-series contract (ADR-189), in
place, with the writer running. One verb, one step at a time; each step
refuses to run out of order, and every step can be rolled back until the old
table is dropped.

```
axi data adopt-timeseries silver.readings                       # estimate: writes nothing
axi data adopt-timeseries silver.readings --apply               # copy by window, verified, resumable
axi data adopt-timeseries silver.readings --apply --max-windows 20
axi data adopt-timeseries silver.readings --swap                # finish the copy, swap names (one short lock)
axi data adopt-timeseries silver.readings --verify              # every window of the old table against the new
axi data adopt-timeseries silver.readings --verify --repair     # re-insert what the new table lacks
axi data adopt-timeseries silver.readings --drop-old            # verify, then drop the old table
axi data adopt-timeseries silver.readings --rollback            # undo, before or after the swap
axi data adopt-timeseries silver.readings --status              # where it stands
```

The contract comes from the node's `[[data_platform.timeseries]]` declaration
when the table is declared there, else from the flags: `--identity a,b,c`,
`--time-column ts`, `--rows-per-day N`, `--chunk-interval 6h`,
`--compress-after 7d`, `--retain 400d`.

## What the estimate says

Rows and bytes from the catalogue (a natively partitioned parent is sized from
its leaves), the time range, the windows, the dependent views that will be
rebound, and the ratio and rate **measured** on a sample copied into a scratch
hypertable and compressed. From those: the expected size, the free space the
copy needs, and an ETA. It names what blocks the adoption: a unique index
without the time column, a foreign key that would follow the old table
through the rename, a trigger, a materialized view, no index that can range
on time (the fix is a BRIN index, built `CONCURRENTLY`).

## How the copy is verified

Each window is one transaction: copy, then compare the row count and an
order-independent hash of the rows (the sum of a 64-bit prefix of each row's
digest) between source and copy, then record the window in the ledger. A
window that disagrees is rolled back and the run stops with it named; nothing
from it is kept. Verified chunks are compressed as the copy advances, so the
copy needs compressed space, not a second copy of the table. The ledger lives
in the database (`data_platform.timeseries_adoptions`), so a fresh call
resumes at the first window not done, and the node's health reads it.

## The swap and the drop

`--swap` copies the tail that arrived since the last window under the only
exclusive lock taken, swaps the names, recreates dependent views (a view
follows the old table's identity through a rename), carries privileges and
sequences, and keeps the old table as `<table>__old`. `--verify` compares
every window of the old table with the new; a digest that differs is resolved
exactly, so rows written since the swap are not a loss and rows missing from
the new table are. `--drop-old` runs that verification first and refuses on a
loss. `--rollback` swaps the names back and carries back what was written
since the swap.

`--tablespace NAME` puts the new hypertable and its chunks on an existing
tablespace; `--lock-timeout` bounds every lock wait (default 5s); `--window`
overrides the derived window width; `--disk-path` names the database volume
when it is not visible from here.
