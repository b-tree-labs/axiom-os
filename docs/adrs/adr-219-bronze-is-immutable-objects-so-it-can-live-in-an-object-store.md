# ADR-219: Bronze is written as immutable objects, so it can live in an object store

**Status:** Accepted (2026-10-29). Amends [ADR-193](adr-193-a-conform-pass-reads-only-what-it-has-not-read.md): its watermark becomes the last batch key read.
**Related:** [ADR-215](adr-215-a-deployment-has-one-size-and-every-size-sees-the-same-cloud-interfaces.md) (object storage over the S3 API at every size; follow-up 3), ADR-015 (`AXIOM_S3_ENDPOINT`), ADR-049 (bronze is the substrate of record), ADR-177 (the ingest edge), ADR-180 (the local medallion and the forwarder), ADR-194 (deposits are bronze)

## Context

ADR-215 puts every size above `micro` on the same object store contract, and a
`large` platform keeps bronze in it: a claim on one node is the wrong home for
the substrate of record of a multi-node cluster, and an HPC center's S3 endpoint
or a cloud's S3 is where its volume lives. Today bronze is a directory tree.

A read-only survey of the data platform (2026-10-29) found:

- **Writes go through two sinks** (`bronze/sinks.py`): blobs are content
  addressed and written by temp-and-rename, which an object store handles
  directly (put if absent).
- **The row lane appends.** `_rows/<day>/<item_id>.jsonl` and the dedup ledger
  `_seen.txt` are opened with mode `"a"`. An object store has no append.
- **The incremental conform depends on files growing in place** (ADR-193): a
  per-file watermark of size, mtime, a head hash and the last line, and a read
  that seeks to the stored offset.
- **Readers walk the tree**: `iterdir`, `glob`, `rglob`, `os.walk`, `stat`,
  across about thirteen modules, none through an abstraction. `S3StorageProvider`
  is file-path based and has no byte-level put/get, ranged get, stat, paginated
  listing or conditional put.

About 25 to 30 files and 50 call sites change whichever way this is decided.
The decision is the row lane, because it fixes the shape of ADR-193's watermark.

## Decision

**Bronze is a set of immutable objects behind one `BronzeStore`, with a
filesystem and an S3 implementation. Nothing in bronze is ever modified after
it is written.**

- A row batch is one object, `_rows/<day>/<item_id>/<batch-seq>-<sha256>.jsonl`,
  written once with put-if-absent. A collector's retry of the same batch is the
  same key, so it lands once.
- The dedup ledger becomes one small object per batch (or the batch's own key,
  which already names its content) instead of an appended file.
- **ADR-193's watermark becomes the last batch key read per item**, in key
  order, instead of a byte offset into a growing file. A batch is read whole
  exactly once; nothing is re-hashed and nothing seeks. "Has this been read" is
  a key comparison.
- Listing uses prefix and delimiter, paginated, so a reader asks for one day of
  one source rather than walking the tree.
- The filesystem implementation keeps today's layout for existing data and
  writes new batches in the new layout, so `micro` and laptops keep working on a
  plain directory, and a migration copies old files into batch objects once.
- The edge's outbox and offsets stay local: they are the edge's working state,
  not bronze.

## Options considered

- **Keep appending locally and flush to the object store when a file rolls.**
  The smallest code change, and ADR-193 stays as it is. It lost because every
  writer then needs a local disk that survives a restart, which is the thing a
  multi-node `large` deployment is trying not to depend on, and the object store
  is always behind the truth by up to one file.
- **Keep bronze on a shared file system** (an HPC center's parallel file system
  mounted on every node, or a ReadWriteMany volume) and use the object store
  only for blobs and backups. It lost because it does not port: a cloud has no
  such mount for free, and ADR-215's point is that the same chart runs there.
- **One object per row.** Simple and immutable, but millions of tiny objects a
  day (one deployment alone lands 2 to 4.6 million silver rows a day) cost more
  in requests and listing than the data is worth.

## Consequences

- Bronze can live in any S3 store at every size, and in a directory where there
  is none, behind one interface.
- ADR-193 is amended: the watermark is a batch key, not an offset. Its
  guarantee (read only what has not been read, never twice) is unchanged and
  simpler to show.
- A row batch is the unit of landing, so a collector's batch size sets the
  object size. The ingest face already batches (`AXIOM_INGEST_MAX_ROWS_PER_BATCH`).
- One migration per existing deployment: copy each jsonl file into batch objects
  and record watermarks at their ends.

**Follow-up work:** the `BronzeStore` interface and two implementations; byte
level operations on `S3StorageProvider` (put if absent, get with range, head,
paginated list with delimiter); the two sinks; the thirteen readers; ADR-193's
watermark; the migration; and the tests that pin today's filesystem layout.
