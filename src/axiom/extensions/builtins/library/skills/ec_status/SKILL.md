---
name: library.ec_status
description: Why a library file is held from retrieval (the controlled code and the kind of file), its export-control review trail, and whether you may release it.
---

# library.ec_status

Why a library file is held from retrieval: the controlled code it belongs to,
the kind of file (input or output) and the extractor that recognised it, with
every decision on it so far (hold, release, keep-held, re-hold: who, when,
why, and whether the record is sealed). It also says whether you may release
it, and why not (ADR-213 D9).

Risk: `read`. Served from this install's library, or from the bound node's when
this install is bound (ADR-212).

## Inputs

- `id`: the library document id
