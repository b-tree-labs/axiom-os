---
name: library.show
description: One library document: where it filed, who filed it, and whether retrieval can find it.
---

# library.show

One library document: where it filed, who filed it, and whether retrieval can find it.

Risk: `read`. Served from this install's library, or from the bound node's
when this install is bound (ADR-212). The caller's site and principal come from
the verified credential; a request cannot name another.

## Inputs

- `id`: the library document id
