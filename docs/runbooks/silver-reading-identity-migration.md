# Runbook: moving a populated silver onto its reading identity (ADR-206)

Use this for a node whose `silver.signals` already holds rows. A fresh install
gets the index from the DDL and needs nothing here. Until you finish step 4,
conform keeps writing on the legacy key, exactly as before, and
`conform-run --rebuild` refuses.

Nothing here runs on its own. Run each step by hand, during a window you
choose.

## 0. Before you start

- Deploy the release that carries ADR-206. Its DDL adds `content_hash`,
  `conform_version` and `conformed_at`. These are nullable with no default, so
  the add is a catalogue edit on a table of any size. From then on every
  conform write records them, which gives the collapse a real "newest conform"
  to keep.
- Take a backup (`axi data backup`). The collapse copies every row it removes
  to `silver.signals_identity_audit` before deleting it, but a backup is still
  the floor.

## 1. Preflight (read-only)

```bash
axi data reading-identity preflight --site <site> --start 2026-10-01 --end 2026-10-08 --json
axi data reading-identity preflight --site <site>          # whole site, once per site
```

The preflight reports:

- per site and feed, the identities with more than one row, the extra rows,
  and how many are **re-sends** (same content) against **divergent** (the rows
  disagree);
- the layout: TimescaleDB version, whether the table is a hypertable, whether
  compression is enabled, and the chunk count;
- the build path and its lock profile, and the plan.

The duplicate scan only reads. It runs as one bounded GROUP BY per window of
`--window-days` (default 31), each in its own read-only transaction under
`SET LOCAL statement_timeout` from `--statement-timeout-s` (default 900), so it
completes on a role whose own `statement_timeout` is 30 s; the role's setting
is never changed. The time extent comes from `gold.signal_catalogue` when it
exists, else from min/max of `ts`; an explicit `--start/--end` is respected.
One line per window goes to stderr (`window 2026-05-01..2026-06-01: N extra`).
`ts` is part of the identity, so a duplicate sits wholly inside one window and
the per-window results add up exactly. If a window times out, the preflight
prints one line naming it; re-run with a smaller `--window-days` (or a larger
`--statement-timeout-s`). On a compressed hypertable the scan ran at about
2.3M rows/s on a laptop container; expect 3 to 5 times slower on a 2-CPU node.
On a plain table without a `ts` index each window still reads the table, so
scope by `--site` as well.

## 2. Decide on divergent identities

A divergent identity is two different values for one reading. The collapse
keeps the newest conform: `conformed_at`, and for rows from before ADR-206,
`row_hash DESC`. `row_hash DESC` is the row `gold.signals_latest` serves today,
so no answer a reader already gets changes. The other rows go to the audit
table. Divergent identities are left in place unless you pass
`--accept-divergent`.

## 3. Collapse

Pause the conform timer first. Rows written between the collapse and the
build could add new duplicates.

```bash
systemctl stop <conform timer>                   # the node's conform-run unit
axi data reading-identity collapse --site <site>                    # dry run: counts only
axi data reading-identity collapse --site <site> --apply            # re-sends
axi data reading-identity collapse --site <site> --apply --accept-divergent   # if decided
```

The collapse runs one day per transaction (`--window-days`), each window under
the same `--statement-timeout-s`, and reports progress per window. Each window
copies its losers to the audit table and deletes them in the same
transaction, so a run that is cut short resumes cleanly. Deletes into
compressed chunks are supported on 2.17.2 and later: only the affected
segments are decompressed. Re-run the preflight until `extra_rows` is 0
across all sites.

## 4. Build the index

```bash
axi data reading-identity build-index --lock-timeout-s 10
systemctl start <conform timer>
axi data reading-identity preflight        # index_present: true
```

The path depends on the layout. Both versions below were measured on
2026-10-27.

| layout | statement | locks | time |
|---|---|---|---|
| plain table | `CREATE UNIQUE INDEX CONCURRENTLY` | SHARE UPDATE EXCLUSIVE: reads and writes continue | two table scans |
| hypertable, TimescaleDB 2.30.2 | `CREATE UNIQUE INDEX` (no CONCURRENTLY, no `transaction_per_chunk` for UNIQUE) | SHARE on the hypertable and every chunk: SELECTs (gold, charts, MCP) continue; writes wait | 2.6M compressed rows in 1.4 s on a laptop container; plan about 9 min per billion rows, 3 to 5 times that on a 2-CPU node |
| hypertable with compression, TimescaleDB 2.17.2 | refused: "operation not supported on hypertables that have compression enabled" | none | upgrade first: `ALTER EXTENSION timescaledb UPDATE` (2.30.2 verified), then build |

On a compressed chunk the index adds almost nothing on disk (110 MB before and
after in the measurement), because the rows live in the compressed table and
uniqueness is checked by decompressing segments. Decompressing every chunk
instead would make the table about seven times larger, so that path is not
supported.

If the build fails on a duplicate, it leaves nothing behind. A failed
CONCURRENTLY build leaves an invalid index, which the next `build-index` drops.
If it times out on a lock, a writer was still running: stop it and retry.
While the index is absent, conform keeps using the legacy key, so a failed
build is never an outage.

## 5. After

- Conform switches to the identity upsert on its next pass, with no restart:
  the writer probes for the index every time it binds.
- Fix wrong gold with
  `axi data conform-run --rebuild --connector <c> --start <day> --end <day+1> --dry-run`,
  then run it again without `--dry-run`.
- Contract, in a later release: drop `signals_pkey` and repoint
  `silver.signal_uncertainty` at the identity. Do this once every node reports
  `index_present`. The `# contract:` marker is in
  `conformance/reading_identity.py`.
