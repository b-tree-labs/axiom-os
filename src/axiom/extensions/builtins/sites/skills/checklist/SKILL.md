---
name: sites.checklist
description: Every ADR-178 readiness check for a site, each ok, waiting, failing (with the fix) or not applicable. Reads only.
---

# sites.checklist

Runs the ADR-178 D4 checklist for one site and reports each check as `ok`,
`waiting`, `failing` (with the fix) or `not applicable`. A site is `ready`
only when every check is ok or not applicable.

Checks answered from the site's heartbeats: heartbeat recent, forwarder
transport, data fresh, node version in the compatibility window, archive disk.
Every check that needs the site registry, the edge's acceptance report or a
report the sender does not yet carry is `failing` and names what is missing.

## First-run mode

With `kind=first_run` (CLI: `sites checklist --first-run`; web: `GET /api/v1/first-run`;
`site` is not needed) it returns exactly five steps, in order, each
`{id, label, state, detail, action: {label, prompt}}` with `state` `done` or `todo`,
plus `done`, `total` and `complete`:

1. `connect_data`: done when a gold dataset is served or a data kit exists.
2. `invite_people`: done when an invitation was issued or more than one person holds a key.
3. `set_branding`: done when the site's name or accent differs from the default.
4. `pick_landing`: done when the `interface.landing` setting is stored.
5. `first_chart`: done when the signed-in person has a saved chart.

Every probe reads only. A source that cannot be read leaves its step `todo` and says so.
