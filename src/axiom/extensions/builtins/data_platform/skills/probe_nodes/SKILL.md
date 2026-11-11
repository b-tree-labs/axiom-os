---
name: data.probe_nodes
description: Check every node the platform can reach from outside (its /readyz), and keep the result for the uptime report, so a node that keeps beating while unreachable reads as a network outage.
---

# data.probe_nodes

The heartbeat says a node is up from the inside. This is the outside view: for
each node whose heartbeat advertises a `probe_url` (from the node's
`AXIOM_PUBLIC_URL`), a GET of its `/readyz`, or `/healthz` on an older node.
Each result is kept per site and node, and the uptime report uses it to tell
"the node was down" from "the node was up and could not be reached".

Push-only nodes advertise no address and are not checked; their outside
observer is when their heartbeats arrive.

The orchestrator runs it every minute on the platform node. Run it by hand to
see each reachable node's state now.
