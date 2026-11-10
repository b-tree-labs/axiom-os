---
name: library.status
description: What happened to one file you uploaded: every pipeline stage, what each extractor found, where each output went, the export-control verdict and where the Data Book filed it.
---

# library.status

What happened to one file you uploaded: every pipeline stage (received, stored,
identified, extracted, conformed, indexed), what each extractor found, where each
output went, the export-control verdict and where the Data Book filed it, with
the reason (ADR-213).

Risk: `read`. Served from this install's library, or from the bound node's when
this install is bound (ADR-212).

## Inputs

- `id`: the library document id
