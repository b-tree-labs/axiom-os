# ADR-193: A conform pass reads only what it has not read

**Status:** Proposed
**Date:** 2026-10-15
**Related:** ADR-128 (tier boundaries: bronze lands raw, silver conforms), ADR-131 (there is always a medallion), ADR-138 (no heavy orchestrator), ADR-174 (Postgres is the runtime database), ADR-189 (time-series storage), spec-signal-ingest-and-producer

## Context

The bronze→silver conform pass walked every connector's `_rows` files and
read each one whole, every time it ran. That is correct and idempotent (the
silver upsert is `ON CONFLICT DO NOTHING` on the row's identity), and it does
not scale with the archive: on one node the nightly pass re-read 30.4 million
bronze rows for 94.6 million no-op upserts, took 8 h 12 min with a peak of
127 GB of memory, and globbed its file list at the start, so a frame landing
after it began waited a day. The site's script and the platform's pass shared
the shape of the problem: no memory of what had been read.

Bronze files are append-only by contract (the ingest sink lands one JSON
record per line and only ever appends), and almost all of them never change
again. The pass had nothing to hold that fact.

## Decision

**The conform pass keeps a durable watermark per bronze file, in the
database, committed in the same transaction as the rows it accounts for; it
reads only the bytes past the watermark, streams what it reads, runs on a
short schedule, and rebuilds silver only when told to.**

### D1 — A watermark per file, in the database

`data_platform.conform_watermarks` holds, per bronze root and file: the
file's size and mtime when last read, the byte offset conformed, a hash of
the file's head (at most 4 KiB, never past the offset) and a hash of the last
complete line consumed. It lives in the database rather than beside the files
because it must move with the rows: it is written at every checkpoint, just
before the commit, so a pass cut short resumes at its last commit and a row
is never counted as conformed before it is.

### D2 — Only new bytes are read, and a rewrite is noticed

A file whose size and mtime are unchanged and whose watermark reaches its end
is not opened. A file that grew is opened once to check its head and its last
consumed line, then read from the offset. A file that shrank, or whose head or
last line no longer read as recorded, was rewritten: it is read again from the
start and reported. Bronze being immutable by rule, the rewrite is conformed
and never used to change or delete what silver already holds. A trailing line
without its newline is a record still being written and waits for the next
pass.

### D3 — Streaming, bounded memory

Files are read a line at a time, never whole. The pass's memory is bounded by
the batch, the validator's one buffered instant, and the per-channel
catalogue; it does not grow with the largest file.

### D4 — The walker checkpoints, outermost layer first

Every `checkpoint_rows` lines the walker flushes the validator, then the
batched writer, persists the watermarks, and commits. The walker owns the
checkpoint because it knows the offsets, and the order matters: a watermark
must never run ahead of a row still buffered in a layer above the database.

### D5 — A short schedule, not a nightly one

`axi data conform-run --watch <seconds>` keeps passing, re-listing the files
each pass, so a file landing after a pass starts is conformed by the next one.
The scheduled run remains available; what it does on a node is the site's
declaration.

### D6 — A rebuild is explicit

`--rebuild` forgets every watermark of the root and reads everything again,
re-deriving rows already written (it implies `rederive`): the one way to
rebuild silver from bronze, and never a side effect. `--full` is the old
read-everything pass, kept for comparison and for a database that holds no
watermarks on purpose.

## Options considered

- **A watermark file beside the bronze files.** Cheap, and wrong at the one
  moment it matters: a pass that commits rows and then dies before writing
  the file re-reads them (harmless), while one that writes the file and then
  dies before the commit skips them for good. In the database, in the same
  transaction, neither can happen.
- **A content hash of the whole file.** Proves nothing cheaper than
  re-reading the file, which is what this exists to avoid. The head and the
  last consumed line, plus size and mtime, catch every rewrite that changes
  what was read and cost one open.
- **Reading by file mtime alone.** A rewrite can preserve mtime and size;
  a resumed pass sits mid-file with both unchanged. The shortcut applies only
  to a file whose watermark reaches its end.
- **A rebuild on every schema change, implicitly.** The failure it would
  reintroduce is the nightly re-read. A rebuild is a decision.

## Consequences

- A pass over an unchanged archive reads nothing and says so (files seen,
  new, grown, rewritten, unchanged; bytes read).
- The nightly eight hours become a pass whose cost is the day's new bytes,
  on a schedule short enough that nothing waits a day.
- The first incremental pass over an existing archive reads everything once
  and seeds the watermarks; that is the migration, and it needs no window.
- Follow-up: the site script that duplicated this pass retires in favour of
  `axi data conform-run` once the node carries this release.
