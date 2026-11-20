---
name: library.ec_rehold
description: Take a release back: the file's text leaves retrieval and it is held again. An ec-reviewer or the uploader may; the reason is required and the decision is attested.
---

# library.ec_rehold

Take a release back: the file's text leaves retrieval and it is held again.
Holding is the safe direction, so an `ec-reviewer` or the file's uploader may.
The decision joins the site's sealed review chain (ADR-213 D9).

Risk: `reversible-write`. Served from this install's library, or from the bound node's when
this install is bound (ADR-212).

## Inputs

- `id`: the library document id
- `reason`: one line saying why it is held again (required)
