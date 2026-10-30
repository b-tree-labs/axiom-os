---
name: library.move
description: File a document in another Data Book folder. The move is remembered, so later similar documents file there too.
---

# library.move

File a document in another Data Book folder. The move pins the document there
(a re-extraction does not re-file it) and is recorded as a correction, so later
documents like it file there too (ADR-213 D11).

Risk: `reversible-write`.

## Inputs

- `id`: the library document id
- `folder`: the folder's path in the Book (from `GET /api/v1/library/book`)
