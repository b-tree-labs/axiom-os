---
name: library.reextract
description: Run a document's extractors again from the bytes bronze kept (all, named ones, or only those whose version changed); the new results replace the current ones and every route is offered them.
---

# library.reextract

Run a document's extractors again from the bytes bronze kept: all that match,
the named ones, or only those whose installed version differs from the one that
ran (`stale_only`). The new results replace the current ones in place, older
results stay as history, and every route is offered the new results (ADR-213 D8).
A document you moved stays where you put it.

Risk: `reversible-write`. Only the person who filed the document re-extracts it.

## Inputs

- `id`: the library document id
- `extractors`: extractor names to re-run (default: all that match)
- `stale_only`: only extractors whose version changed
