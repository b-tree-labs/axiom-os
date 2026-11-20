---
name: library.access_expiry
description: KEEP's hourly tick for Library access that ends: one reminder a week before to the person who gave it, and the entry ended on its day, with the end in the file's activity.
---

# library.access_expiry

KEEP owns access expiry (prd-sharing-and-access SHR-R7.1; ADR-209 D8a). The
node runs this every hour (`library_access_expiry` schedule):

- A week before an access entry ends, the person who gave it is told once, by
  name of the person and the file, with the day.
- On its day the entry ends, and "<person>'s access ended on its day" is a line
  in the file's (or folder's) activity, by `@keep`.

Risk: `reversible-write`.

## Inputs

None.
