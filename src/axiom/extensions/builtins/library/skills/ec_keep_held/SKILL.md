---
name: library.ec_keep_held
description: Record that an ec-reviewer looked at a held file and keeps it held. The reason is required and the decision is attested.
---

# library.ec_keep_held

Record that an `ec-reviewer` looked at a held file and keeps it held. Nothing
changes for retrieval; the decision and its reason join the site's sealed
review chain, so "nobody has looked" and "a reviewer looked and said no" are
told apart (ADR-213 D9).

Risk: `reversible-write`. Served from this install's library, or from the bound node's when
this install is bound (ADR-212).

## Inputs

- `id`: the library document id
- `reason`: one line saying why it stays held (required)
