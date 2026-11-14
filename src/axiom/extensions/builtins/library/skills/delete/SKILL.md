---
name: library.delete
description: Remove a document you filed from retrieval and every listing. Its bytes stay in bronze behind a tombstone, so library.restore can bring it back.
---

# library.delete

Remove a document you filed from retrieval and every listing. Its bytes stay in bronze behind a tombstone, so library.restore can bring it back.

Risk: `reversible-write`. Served from this install's library, or from the bound node's
when this install is bound (ADR-212). The caller's site and principal come from
the verified credential; a request cannot name another.

## Inputs

- `id`: the library document id
