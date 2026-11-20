# ADR-212: A library upload is filed on the node that holds the library, and a delete keeps the bytes

**Status:** Draft (2026-10-28), for review
**Related:** [ADR-207](adr-207-one-serving-mode-for-every-surface-a-bound-node-forwards-shared-reads.md) (one serving mode; a bound node forwards shared reads), [ADR-106](adr-106-push-ingest-gateway.md) (push ingest; site from the credential), ADR-049 (bronze is the substrate of record), ADR-052 (schema per extension), ADR-150 (Book is the Library), ADR-209 draft (sites, visibility as tuples), ADR-174 (Postgres at runtime), [spec-library](../../src/axiom/extensions/builtins/library/docs/spec-library.md)

## Context

The library takes uploads from a person (the Book page, a chat attachment, the
CLI, an agent) and turns each into a filed document that retrieval can find:
bronze bytes, a catalog row, and chunks in a corpus. Two questions have answers
that are expensive to change once documents exist.

1. **Where a bound install's upload goes.** ADR-207 makes a bound install a
   window onto its site node and says what it forwards: shared *reads*. "Nothing
   here can write" is the rule the web app's bound forwarder states. An upload is
   a write. If it lands on the laptop, the person believes they shared a document
   with their site and nobody else can see it; the laptop's own retrieval store
   is not the one the site's chat reads. If it lands on the node, the laptop must
   send a write with the binding's key, which ADR-207 did not provide for (the
   kit submission was the one existing write, on its own path).
2. **What a delete does.** A document is in three places. Retrieval must stop
   showing it at once (a person deleting a document is usually withdrawing it).
   Bronze is the substrate of record (ADR-049), and other pipelines treat a
   bronze blob as permanent.

## Decision

**A library upload is filed on the node that holds the library.** On a bound
install every library call, from any surface, is relayed to the bound node's
`/api/v1/library` with the binding's key, offered only to that node (the same
key resolution data reads use). The node derives the site and principal from
the key and files the document as it would for someone signed in there. The
bound install keeps no bytes, no row and no chunks. An unreachable node is a 503
naming the site and the node; nothing is queued or kept locally.

**A delete removes the document from retrieval and listings immediately and
keeps its bytes.** Its chunks are deleted from its corpus, its catalog row
becomes a tombstone (`deleted_at`, `deleted_by`), bronze gains a `_deleted`
sidecar, and an event records who did it. The content blob stays in bronze, so
a restore re-indexes from it. Purging bytes is an operator's retention action
and is not part of delete.

The node's library is its own mount nested in `/api/v1`, so the serving stack's
authz hook decides every request; a session and a gate key reach the same
handlers. A bound laptop's key needs a scope that covers the `library` mount for
writes.

## Options considered

- **File locally, sync to the node later.** Gives offline uploads, but the
  document is invisible to the site until a sync nobody watches succeeds, and a
  conflict between two copies has no owner. Rejected for the same reason ADR-207
  refuses to substitute local data.
- **Upload directly from the browser to the node.** Puts the key in the browser,
  which ADR-207 forbids, and needs CORS on the node.
- **Hard delete.** Simple, but loses the audit trail and the restore, and
  removing a bronze blob breaks whatever else derived from it.
- **Soft delete that leaves the chunks.** Keeps retrieval answering from a
  document its owner withdrew.

## Consequences

- ADR-207's "a bound node forwards shared reads" gains one write lane: the
  library's. It is the library's mount only, not a general write forwarder.
- Gate keys issued for binding need a `library` scope to upload; read-only keys
  can list, preview and search.
- A node that serves the library needs a body limit on its front door at least
  the library's batch cap, and a library bronze root on durable storage.
- Bronze grows by every upload, including deleted ones, until a retention policy
  (follow-up) purges tombstoned blobs no live document references.
