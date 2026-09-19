# ADR-020: Federation Identity Layers & Relationship Taxonomy

**Status:** Accepted  
**Date:** 2026-04-13  
**Deciders:** Benjamin Booth  
**Related:** ADR-016 (Multi-node federation), ADR-018 (self-hosted node public endpoint), ADR-019 (Node profiles), ADR-021 (Threat model)

---

## Context

The federation design has accumulated requirements that the simple "each node belongs to an org" model cannot support:

- People do work for multiple orgs simultaneously (joint appointments, consulting, industry + academic affiliations)
- People use one laptop across all their affiliations
- Orgs nest (org-system → an institution → school → department)
- Orgs have legitimate legal claims on work products produced under their affiliation, even though the person is portable across affiliations
- Same-org nodes need to fail over to each other; cross-org nodes should not
- Federation needs to distinguish tight intra-org coupling from arm's-length partnerships from open community participation

Additionally, the original candidate vocabulary ("Sibling / Peer / Public") was invented and informal. Analogous patterns from databases, Kubernetes, academic federation (InCommon), and ActivityPub suggest standard terminology is better.

## Decision

### Four Identity Layers

```
1. Platform identity  — A person or org, globally unique, cryptographically owned
2. Node identity      — A specific compute instance (laptop, server, cluster), owned by a platform identity
3. Affiliation        — A signed assertion: "platform identity X is member of org Y with role R until T"
4. Context            — An active (platform identity + affiliation + project + node) combination
```

**Platform identity is portable.** A person (e.g., `@ben.booth:axiom`) owns their Ed25519 root key, their reputation, their personal corpus, and their history. This survives employer changes, institutional splits, and retirement. Institutions do not own personal platform identity.

**Artifacts produced within a context may be owned by the context's org.** Portability of identity does not imply portability of every artifact. Affiliation agreements declare IP/data ownership per context. Ben's platform identity is his; a research paper he wrote under his UT affiliation may be UT's intellectual property. This separation placates institutional legal claims without compromising the identity model.

**Nodes are owned by platform identities, not by orgs directly.** A personal laptop is owned by the human. A self-hosted node is owned by the institution (the org's institutional platform identity). Nodes can host multiple contexts simultaneously with strict isolation.

**Context switching is explicit and frequent.** A context determines which RACI applies, which RAG tiers are accessible, where findings get promoted, which agent instances are active, and which federation keys sign outbound messages. `axi context list` and `axi context switch <name>` are first-class CLI operations.

### Principal Naming Convention

Every principal (human, agent, node, org) is addressed as `@name:context` (Matrix-style). A single leading `@` is mandatory; the `:context` suffix is optional and omitted for the principal's home context.

- `@ben.booth` / `@ben.booth:axiom` — human (platform identity root)
- `@ben-curio` — agent in personal context
- `@ben-curio:ut-austin` — agent in UT context
- `@example-host:org` — node principal
- `@ut-austin` — org principal
- `@all-curios:<period-id>` — wildcard scoped to a classroom period

Email-style `user@domain` and fediverse-style `@name@server` are deliberately *not* used: the colon separator keeps principals visually distinct from email addresses and parses unambiguously against names that contain dots (`ben.booth`). Convention borrowed from Matrix, the federated chat protocol with analogous identity-portability requirements.

### Agent Identity Resolution

Agents belong to contexts, not to bare platform identities:

- `@ben-curio` (unqualified) — personal-context agent
- `@ben-curio:ut-austin` — UT-context agent with UT corpus and RACI
- `@ben-curio:soilmetrix` — Soilmetrix-context agent with that org's corpus and RACI

Across federation, only qualified forms are addressable. Agent instances can physically share a node (Ben's laptop hosts both `@ben-curio` and `@ben-curio:ut-austin`) but run in isolated context workspaces.

### Federation Relationship Taxonomy

Three relationship types govern how contexts interact across federation:

```
Cluster  — Same-org contexts with shared trust, replicated public/org-tier data, 
           failover enabled. Example: a self-hosted node (org-Server) + an HPC cluster (org-Platform).
           
Partner  — Cross-org contexts with bilateral trust agreement, scoped data sharing 
           per access tier, no failover. Example: an institution ↔ a partner-lab research partnership.
           
Federated — Open federation membership, public-tier data only, no bilateral 
            agreement. Example: an institution ↔ any participating institution.
```

Selected from candidates (Sibling/Peer/Public; Cluster/Partner/Public; Local/Trusted/Public; Home/Partner/Federated) via rubric-scored LLM-as-judge evaluation. Winner on semantic precision, composability, and familiarity from existing vocabulary (database clusters, email federation partners, academic federation members).

Relationships support:
- **Quarantine** (new-peer probation) — all relationship types start here
- **Lifecycle metadata** — expiry dates for time-bounded collaborations, suspension for pauses
- **Extensibility** — additional types like Observer (read-only) can be added without breaking the taxonomy

### Nested Orgs and Affiliation Chains

Orgs form a DAG, not a flat set. Affiliations carry the org chain:

```yaml
affiliation:
  subject: "@ben.booth:axiom"
  org_chain: [org-system, org-institution, school, department]
  roles: [researcher]
  signed_by: cockrell-school-identity-authority
```

Default relationship inheritance:
- Child-to-parent: **Cluster** by default (inherits parent trust context)
- Sibling orgs (shared parent): **Partner** by default
- Unrelated orgs: **Federated** peer

Orgs can override defaults via federation policy.

### Multi-Affiliation Humans

Ben Booth may have simultaneous affiliations with UT (researcher), INL (contractor), and Soilmetrix (founder). Each affiliation is a separate signed assertion on his *same* platform identity. Contexts on his laptop give him an isolated workspace per affiliation. Federation peers address his org-specific agents; his personal identity underlies them all but is not addressable across federation without a context qualifier.

When an affiliation ends (Ben leaves UT), the affiliation assertion is revoked. UT-context workspaces on Ben's laptop become read-only (preserving Ben's personal access to his own work history per the alumni model). The `@ben-curio:ut-austin` agent stops responding. Ben's personal identity and other affiliations are unaffected.

### Verification Chain for Federation Messages

Every federation message carries three signature layers:

```
1. Node signature (ben-laptop-mbp-node-key)     — "this message came from this node"
2. Context attestation (within node's capability) — "running context X for identity Y under affiliation Z"
3. Affiliation validity (signed by org authority) — "Y's affiliation with org Z is valid until T"
```

Peers verify all three before trusting content. An attacker forging any layer needs private keys they don't have.

## Consequences

**Positive:**
- Matches reality for multi-org researchers, joint appointments, student consultants
- Nodes don't lock people into single-org usage
- Portable identity preserves career continuity and personal autonomy
- Institutional legal claims are placated via context-scoped artifact ownership, not identity capture
- Federation relationship taxonomy uses familiar infrastructure vocabulary
- Supports org hierarchies, temporary collaborations, and edge cases without special-casing
- Layered signature verification resists identity spoofing and affiliation forgery

**Negative:**
- More concepts to learn and implement than a flat "node = org" model
- Context switching adds cognitive overhead (mitigated by making it visible and cheap)
- Three signature layers per federation message is heavier than one
- Key management is harder (platform root key, node key, org authority keys)

**Neutral:**
- Relationship taxonomy (Cluster/Partner/Federated) is decided; identity root authority (self-sovereign vs academic federation vs web of trust) is deferred to a future ADR.

## Open Questions

Deferred to implementation or future ADRs:
- **ADR-022 (Federation Identity Roots):** which identity authority model for institutions — self-sovereign, InCommon/eduGAIN leverage, or domain-based TLS? (Deferred pending pilot with UT)
- Key rotation protocol for platform identities
- Affiliation revocation propagation latency tolerance
- Cross-context data references (explicit consent protocol for personal → org or vice versa)

---

*See also: ADR-019 Node Profiles, ADR-021 Federation Threat Model.*
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
