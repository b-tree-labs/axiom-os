---
name: library.transfer
description: Make someone already on a Library file's or folder's access list its Owner; you become an Editor and it stays where it is filed.
---

# library.transfer

Ownership transfer (SHR-R5.1). The new Owner must already be on the access
list; the old Owner becomes an Editor; the file stays where it is filed and the
new Owner is told. Transferring a folder does not transfer what it holds. A
person's act at a terminal or in the page, so it is a CLI verb only.

Risk: `reversible-write`.

## Inputs

- `id`: a file id, or `folder:<path>`
- `to`: the new Owner, `@name:<site>`
