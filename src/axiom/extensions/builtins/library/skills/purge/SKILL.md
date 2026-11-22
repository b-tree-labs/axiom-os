---
name: library.purge
description: Delete a document forever, from Deleted files, only after its retention floor and outside any hold.
---

# library.purge

Destroys a removed document: the row, its versions and everything derived from
the bytes, and the bytes themselves unless another item still points at them.
Refused under an export-control hold and, with the labels on, before its
retention class's floor (operational: five years), for a lifetime item and
under a legal hold; the refusal names the floor and the date it ends. Every
attempt, refused or done, is written to the action ledger and as a `purge`
library event, and a `<id>.purged.json` record stays beside the bytes
(MVP-22-A2).

CLI only: an agent never deletes forever.

Risk: `destructive`.

## Inputs

- `id`: the library document id
