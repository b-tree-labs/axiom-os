---
name: library.list
description: List the library documents you may see at your site, newest first, with filters.
---

# library.list

List the library documents you may see at your site, newest first, with filters.

Risk: `read`. Served from this install's library, or from the bound node's
when this install is bound (ADR-212). The caller's site and principal come from
the verified credential; a request cannot name another.

## Inputs

- `q`: match on title, file name, label, collection or tag
- `category`: only this category
- `collection`: only this collection
- `tag`: only documents with this tag
- `visibility`: site or private
- `status`: indexed, pending, unsupported or failed
- `deleted`: list your deleted documents instead
- `limit`: at most this many (default 200)
