---
name: library.request
description: Ask the Owner for access to a Library file or folder (works from its address alone, which reveals nothing), see or withdraw your request, remind once a day; or, as someone who may share it, list its requests and approve (with the role you choose) or decline with a reason.
---

# library.request

Request access (SHR-R6). From the address alone: the answer names no title,
Owner or folder. The Owner is told in their Needs you; editors who may share may
answer too. Approving gives the role chosen, which may be lower than asked;
declining needs a reason, which the requester sees as written, with who and when
(`status` shows it as "previously refused"). A request grants nothing until it
is answered. One reminder a day.

Risk: `reversible-write`.

## Inputs

- `id`: a file id, or `folder:<path>`
- `action`: ask (default), status, withdraw, remind, list, approve, decline
- `role`: the role asked for or given
- `note`: a note to the Owner
- `request_id`: the request, for withdraw, remind, approve, decline
- `reason`: why, when declining
