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
