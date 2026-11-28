# ADR-255: A node is local, site or edge; its capabilities and links say the rest

**Status:** Accepted (2026-11-10) · **Amended** (2026-11-24): `site` is no longer a node kind; locality and profile replace the kind, and edge obligations E1 to E7 apply. D1, the `multi-tenant` topology in D4 and the `bind` wording in D3 are revised; see [Amendment (2026-11-24)](#amendment-2026-11-24-locality-and-profile-replace-node-kinds). D2 stands.
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

## Amendment (2026-11-24): locality and profile replace node kinds

Approved by Ben on 2026-11-24. Where this section and the decision above differ, this
section wins.

### A1. `site` is not a node kind

A site is the owner and tenancy boundary: the place whose people, rights and system of
record a node serves. It is not a kind of node. "Site node" in the text above now means a
`full` node that serves a site.

### A2. Two attributes replace the kind

- **Locality: `local` or `remote`.** Relative to the viewer and never stored: not on the
  node, not on its card, not in a topology file. My laptop is local to me and remote to you.
- **Profile: `full` or `edge`.** Intrinsic to the node, declared by it and recorded on its
  card and in the topology. Capabilities (D2) still compose on top of the profile.

### A3. Edge obligations

A node with the `edge` profile:

- **E1** operates offline for long periods: it stores and forwards, and every operation it
  supports works while disconnected;
- **E2** runs on constrained resources and declares its budget (disk, memory, compute) so
  its parent site can plan for it;
- **E3** sits near its data source and collects from it;
- **E4** is a leaf: it never accepts a binding, never hosts another party's site, and is
  never a federation hub;
- **E5** holds only narrow, short-lived credentials issued by keep (ADR-218) for its one
  parent, so losing the device means revoke and reissue, with no shared key to rotate;
- **E6** syncs to exactly one parent site, directly or through a relay;
- **E7** carries a reduced required skill set (an edge core), not the full root core set.

### A4. Links: a binding versus federation

- `bind` is a binding: a principal on a local install reaches a remote `full` node under
  that principal's own grant. It is not federation, and it never targets an edge (E4).
- `send` and `fetch` are federation links of narrow scope: signed, covered by an accord
  limited to the data intents (ingest and outbox read), and governed inbound like any other
  federation traffic. An edge's only link is `send` to, or `fetch` by, its parent site's
  node.
- A relay is a `full` node whose only capability is `ingest`.

### A5. Multi-tenant is a property of the host site

`multi-tenant` is no longer a topology. It is a property of a site's host node (it serves
several sites), recorded in the site registry (ADR-178). The generic topologies are `solo`,
`team`, `collect` and `relay`, each a small graph of nodes (name, profile, site,
capabilities) and links.

### A6. Code follows in a later chunk

This amendment changes the vocabulary only. The code migration (the dev topology moving
from kind to profile, `axi dev up --kind`, the topology schema, and tests for E1 to E7) is
the future chunk C24 of the AEOS 1.0 plan. Until it lands, the current code and
`docs/conventions/local-run.md` still use the old kinds `local`, `site` and `edge`; read
`local` as a `full` node used locally, `site` as a `full` node serving a site, and `edge`
as the `edge` profile.

The full model, the draft AEOS edge profile conformance text and the list of files the
migration touches are in the concept note
(`docs/aeos/1.0/nodes-binding-federation-site-tenancy.md`, private) and decision D17
(`docs/aeos/1.0/decisions.md`, private; Proposed).
