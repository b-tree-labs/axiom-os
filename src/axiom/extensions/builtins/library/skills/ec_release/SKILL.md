---
name: library.ec_release
description: Release one held file version for retrieval. Needs the site's ec-reviewer role; the uploader can never release their own file. The reason is required and the decision is attested.
---

# library.ec_release

Release one held file version for retrieval. The caller must hold the site's
`ec-reviewer` role (the site's role assignments, the FGA `ec_reviewer`
relation, or the site's configured reviewers; with none named, the node's
owner). The person who uploaded the file can never release it.

The release covers these bytes (`sha256`) for this basis (code and reasons). A
new version of the file is checked again, and a re-extraction that finds a new
reason to hold re-holds it. The decision is appended to the site's sealed
review chain; a release that cannot be sealed is refused (ADR-213 D9).

Risk: `reversible-write`. Served from this install's library, or from the bound node's when
this install is bound (ADR-212).

## Inputs

- `id`: the library document id
- `reason`: one line saying why it may be released (required)
