---
name: library.label
description: Show, confirm or change a document's export-control label and its retention class.
---

# library.label

Every Library item carries an export-control label (`public`, `internal`,
`regulated`, `controlled`) and a retention class (`system`, `operational`,
`qualification`, `lifetime`, `legal_hold`). The export-control hook proposes a
label; a person confirms it. With the `library_labels` flag on, an item without
a confirmed label is not ingested, retrieved or exported (MVP-22, MVP-16-A2).

With no `label` or `retention_class` it answers what the label is ("what's this
file's label"). A retention class may lengthen, never shorten; a legal hold ends
only with `release_hold`.

Risk: `reversible-write`.

## Inputs

- `id`: the library document id
- `label`: the export-control label to confirm
- `retention_class`: the retention class
- `release_hold`: true to end a legal hold
