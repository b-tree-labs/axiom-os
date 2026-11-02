---
name: library.restore
description: Bring back a document you deleted, re-indexed from the bytes bronze kept.
---

# library.restore

Bring back a document you deleted, re-indexed from the bytes bronze kept.

Risk: `reversible-write`. Served from this install's library, or from the bound node's
when this install is bound (ADR-212). The caller's site and principal come from
the verified credential; a request cannot name another.

## Inputs

- `id`: the library document id
