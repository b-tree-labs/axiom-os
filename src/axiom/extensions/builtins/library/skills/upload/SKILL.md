---
name: library.upload
description: File a document in the library: guarded, landed in bronze, catalogued, classified and indexed for retrieval. On a bound install it is filed on the bound node.
---

# library.upload

File a document in the library: guarded, landed in bronze, catalogued, classified and indexed for retrieval. On a bound install it is filed on the bound node.

Risk: `reversible-write`. Served from this install's library, or from the bound node's
when this install is bound (ADR-212). The caller's site and principal come from
the verified credential; a request cannot name another.

## Inputs

- `path`: a local file to upload
- `file_name`: the document's name (with content_base64, or to rename a path)
- `content_base64`: the document's bytes, base64, when there is no path
- `visibility`: site (default: everyone granted at the site) or private (only you)
- `title`: a display title (default: the file name)
- `tags`: tags, as a list or comma-separated
- `collection`: a collection to file it in
