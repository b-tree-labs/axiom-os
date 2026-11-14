---
name: library.update
description: Change a document you filed: title, tags, collection, or visibility (which moves its chunks to the corpus the new audience reads).
---

# library.update

Change a document you filed: title, tags, collection, or visibility (which moves its chunks to the corpus the new audience reads).

Risk: `reversible-write`. Served from this install's library, or from the bound node's
when this install is bound (ADR-212). The caller's site and principal come from
the verified credential; a request cannot name another.

## Inputs

- `id`: the library document id
- `title`: a new title
- `tags`: the full tag list (replaces the old one)
- `collection`: the collection, or empty to clear it
- `visibility`: site or private
