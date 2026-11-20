---
name: library.offboard
description: When a person leaves the site, hand every Library file and folder they own to a named person: a site admin's or access admin's step, recorded, and the new owner is told. It opens nothing for the admin.
---

# library.offboard

The offboarding step for the Library (prd-sharing-and-access SHR-R5.3). A
site-admin or an access admin names who is leaving and who takes their files;
every file (live or in Deleted files) and folder the person owns at the site
gets the named person as its Owner, with no second approval. Entries others
hold stay, so nobody loses access they had through those files. Each transfer
is a GUARD receipt and a line in the file's activity; the new Owner is told.
Running it gives the administrator no access to any of it (ADR-142 rule 8).

Risk: `reversible-write`.

## Inputs

- `person`: who is leaving (email or @handle)
- `to`: who takes their files (email or @handle)
