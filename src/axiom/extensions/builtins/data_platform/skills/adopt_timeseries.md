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

## A partition is not adopted on its own

If the table is a partition of another table (or an inheritance child), every
reader and writer goes through the parent, and a TimescaleDB hypertable cannot
be attached to a parent. Adopting the partition alone would swap its name and
leave the parent reading the old table. So estimate, `--apply`, `--swap` and
`--schedule` refuse, and say which parent to adopt and the exact command:

```
$ axi data adopt-timeseries public.readings_default --identity site,channel
blocked: public.readings_default is a partition of public.readings, ... Adopt the parent instead:
axi data adopt-timeseries public.readings --identity site,channel
```

Adopting the **parent** works end to end: the estimate counts every partition,
`--apply` copies by time window across all of them (the DEFAULT partition
included), `--swap` renames the parent to `<parent>__old` with its partitions
still attached and the hypertable to the parent's name, rebinding views and
carrying grants, sequences, owner, comment and column defaults; `--verify`
compares across all partitions; `--rollback` restores the partitioned original
exactly; `--drop-old` re-verifies, then drops the old parent with its
partitions. Each partition needs an index leading with the time column (the
estimate names any that lack one; build it `CREATE INDEX CONCURRENTLY` on the
partition). `--rollback` is never refused. A copy begun before this rule existed is stopped
at its next `--apply` or `--swap`; roll it back and adopt the parent.

`--swap` also proves, before it commits, that a read through the table's name
(and through its parent, and through every dependent view it rebound) scans the
new table and none of the old. If one would not, the swap is rolled back in its
own transaction, nothing changes, and no "next:" line is printed.

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

`--swap` first catches the tail that arrived since the last window up **outside
any lock** (passes that each copy only what the new table lacks, until one
copies fewer than 20,000 rows; a writer that outpaces it is refused, not
locked for), then takes the only exclusive lock for the last few rows and the
renames, and prints the measured hold time. It swaps the names, recreates dependent views (a view
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

## Unattended

`--schedule` (with `--every 5m`, `--batch-seconds 30`, `--ready-timeout 60`) installs the copy as a
job the data orchestrator fires: one bounded batch of verified windows per fire, resumed from the
ledger after any interruption, paused by itself when every window is copied and verified. The swap,
`--verify` and `--drop-old` stay explicit. `--unschedule` removes the job and leaves the copy alone.
Set `AXIOM_ADOPT_DSN` to the database's own address, not a load balancer with an idle timeout.
Run again with `--schedule` to resume a job that halted on a window that would not verify.
