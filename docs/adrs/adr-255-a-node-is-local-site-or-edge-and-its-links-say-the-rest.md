# ADR-255: A node is local, site or edge; its capabilities and links say the rest

**Status:** Accepted (2026-11-10)
**Related:** ADR-019 (node profiles), ADR-016 and ADR-023 (federation topology), ADR-130 (federated site identity), ADR-177 (the ingest edge), the site as the universal deployment unit (ADR-050 and the site design), `docs/conventions/local-run.md`

## Context

We described deployments with the vocabulary of the first one we ran: an "owner" node, an
"ingest" node, an "edge". Those words came from one facility's arrangement, and they mix three
different questions:

- who a node serves (one person, or a shared place);
- where it sits in the flow of data (next to the sources, or where the data lives);
- what it does (serve an app, collect, accept pushes).

"Owner" does not say owner of what. "Ingest" names a mechanism, not a kind of node. A
deployment on a farm, in a classroom or in a factory has the same questions and none of those
words. We need names that hold for any of them, so that a developer can simulate any shape
locally and the platform can document one model.

## Decision

**A node is one of three kinds. What else it does is its capabilities, and how it relates to
other nodes is its links.**

### D1 — Three kinds

| Kind | Serves | Notes |
|---|---|---|
| `local` | One person, on their own machine | A personal site by default (nothing runs un-sited). It may *bind* to a site node to read its data. |
| `site` | The people of a site | Holds the site's system of record (gold), enforces its rights boundary, may host several tenants. |
| `edge` | A site, from the boundary where its data originates | A site node that collects from local sources and **sends outward**, store-and-forward, with no inbound exposure. Its data's home is another node. |

An edge node is a site node whose data's system of record is elsewhere. "DAQ" is what an
edge node does, not a separate kind.

### D2 — Capabilities, not more kinds

A node's profile (ADR-019) turns capabilities on: `serve` (the app and its API), `collect`
(from local sources), `ingest` (accept pushed data and hold an outbox), `send`, `fetch`,
`accept-binds`. A public relay that only accepts pushes and holds them for another node to
fetch is a `site` node whose only capability is `ingest`. There is no fourth kind for it.

### D3 — Links, and a home

A link is directed and has one of three verbs:

- `send`: an edge sends its data to another node;
- `fetch`: a node pulls from another node's outbox;
- `bind`: a local node reads a site node's data under the person's own rights.

An edge has a **home**: the node its data is sent to, directly or through a relay. "Home" is
a relationship an edge holds, not a role a node plays.

### D4 — Topologies are small graphs of these

A topology is nodes (name, kind, site, capabilities) and links. Generic ones: `solo` (one
local node); `team` (a site node with local nodes bound to it); `collect` (an edge sending to a
site node); `relay` (an edge sending to a relay that a site node fetches from);
`multi-tenant` (one site node hosting several tenants). A specific deployment is a named
instance of one of these, declared in the repository that owns that deployment, never built
into the platform.

## Options considered

- **Keep owner, edge, ingest.** They describe one facility's arrangement and one of them
  names a mechanism. They would have to be re-explained in every other domain.
- **A kind per capability** (a relay kind, a collector kind, a gateway kind). The set of
  capabilities grows; the set of kinds should not. Capabilities compose, kinds do not.

## Consequences

- `axi dev up --kind local|site|edge [--home <node>]`, with capabilities in the topology file;
  the convention's role and topology rules (`local-run.md`) are written in these terms.
- Documentation, the node chart and the install profile speak of kinds, capabilities and
  links; "owner" and "ingest node" retire as node names (`ingest` remains the capability and
  the platform's existing name for the mechanism).
- Existing code keeps its names (the ingest sink, the edge source kind, the forwarder); this
  decision names how they combine.
