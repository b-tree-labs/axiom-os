---
name: library.search
description: Find library documents by what they say (retrieval over your site's and your own corpus).
---

# library.search

Find library documents by what they say (retrieval over your site's and your own corpus).

Risk: `read`. Served from this install's library, or from the bound node's
when this install is bound (ADR-212). The caller's site and principal come from
the verified credential; a request cannot name another.

## Inputs

- `q`: what to look for
- `limit`: at most this many documents (default 20)
