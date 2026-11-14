---
name: library.share
description: Share a Library file or folder with a person or group at your site, as viewer or editor, optionally until a date; remove someone; set general access (restricted, or everyone at the site with a role); or change its two sharing settings. Not a chat or session share: it changes who may open one Library file or folder, and every change is recorded.
---

# library.share

Share a Library file or folder (Google Drive's model, ADR-263). Not a chat or
session share: it changes who may open one Library file or folder.

- `with` and `role` (viewer or editor; commenter once comments exist), with an
  optional `expires` date within a year, and `message` for the person told.
- `remove` with `with` ends that person's or group's own access.
- `general`: `restricted` (only the people listed) or `site[:role]` (everyone at
  the site). The file's retrieval follows.
- `editors_can_share`, `viewers_can_download`: the Owner's two settings.

Who may: an Editor while "Editors can change permissions and share" is on, else
the Owner; the settings are the Owner's. A share never leaves the site and never
reaches a person whose clearance does not cover the file's confirmed label.
Every act is a GUARD receipt and a line in the file's activity.

Risk: `reversible-write`.

## Inputs

- `id`: a file id, or `folder:<path>` for a folder of yours
- `with`: `@name:<site>` or `group:<id>`
- `role`: viewer (default) or editor
- `expires`: YYYY-MM-DD, within a year
- `remove`: true to remove `with`'s access
- `general`: restricted, site, or site:<role>
- `editors_can_share`, `viewers_can_download`: true or false
- `notify`: false to not tell the person
- `message`: a note for the person told
