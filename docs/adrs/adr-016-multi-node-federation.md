# ADR-016: Multi-Node Federation and Agent Interoperability

**Status:** Accepted
**Date:** 2026-03-31
**Authors:** Benjamin Booth, Claude

## Context

Axiom is designed to be self-sufficient on a single node (laptop, server, K3D
cluster). But real deployments involve multiple axiom instances on a network —
each with its own agents, data, and shared services. When more capable shared
resources come online (community RAG, a larger LLM, a centralized keystore),
individual nodes should discover and adopt them. When multiple users' agents
coexist in shared contexts, they need identity, presence, and access control.

This ADR adopts open standards for node discovery, agent identity,
agent-to-agent communication, federated resource sharing, and ecosystem
interoperability.

## Decisions

### 1. Adopt Google A2A Protocol for Agent-to-Agent Communication

The Agent2Agent (A2A) protocol is an open standard (Apache 2.0, Linux Foundation
governed) for agent interoperability. Each agent publishes an **Agent Card** at
`/.well-known/agent-card.json` describing its name, capabilities, and endpoint.
Communication uses HTTP/SSE/JSON-RPC — standards axiom already uses.

A2A provides:
- **Capability discovery** — Agent Cards (JSON) at well-known URLs
- **Task management** — defined lifecycle states for cross-agent work
- **Context sharing** — agents exchange context without sharing memory
- **UI negotiation** — agents adapt to different interaction modalities

Axiom implements A2A as the protocol for:
- Agent-to-agent communication across nodes
- Agent capability advertisement within a node
- Agent presence in human-interactive contexts

### 2. Adopt MCP for Tool/Context Interface (Already In Use)

Anthropic's Model Context Protocol (MCP) is already used in axiom (ADR-006).
MCP defines how agents access tools and context. A2A and MCP are complementary:
MCP = agent ↔ tool, A2A = agent ↔ agent.

### 3. Agent Identity Standard

Each agent instance is globally unique, identified by:

```
{human_id}:{agent_type}:{version}
```

Examples:
- `user@university.edu:tidy:v0.4.0` — a user's TIDY agent
- `admin@facility.gov:scan:v0.4.0` — a facility admin's SCAN agent
- `system@facility.edu:tidy:v0.4.0` — Facility system TIDY (no human owner)

This maps to:
- **A2A Agent Card** — published at the node's well-known URL
- **OpenFGA authorization tuples** — `user:admin@facility.gov#tidy` can access `resource:community-rag#read`
- **Audit log attribution** — every action records the full agent identity

Display names for human contexts: "Admin's TIDY", "Researcher's SCAN" — the formal
identity resolves unambiguously behind the scenes.

### 4. Node Discovery

Axiom nodes discover each other via:

1. **DNS SRV records** — `_axiom._tcp.domain.edu` → node endpoints
2. **A2A Agent Cards** — each node publishes cards for its agents
3. **Manual registration** — `axi federation add <endpoint>` for private networks
4. **mDNS/Bonjour** — automatic LAN discovery (opt-in)

### 5. Resource Sharing Protocol

When a shared resource appears on the network, leaf nodes should prefer it
over their local equivalent:

| Resource | Local (self-sufficient) | Shared (when available) | Transition |
|----------|----------------------|------------------------|------------|
| LLM | Local Ollama (small model) | Shared GPU server (large model) | Gateway re-routes; local stays as fallback |
| RAG | Local corpus only | Community RAG with domain packs | Merge community facts; local facts remain local |
| Keystore | K8s Secrets (local) | Centralized Vault/SM | Migrate secrets; local stays as cache |
| Auth/IAM | Single-user implicit | Shared OIDC provider | Switch auth backend; local identity migrates |
| Audit | Local HMAC chain | Federated ledger (ADR-002) | Local chain joins federation; history preserved |

**Principle:** offloading to shared resources optimizes leaf nodes for their
unique purpose (processing, interaction) while community resources benefit from
aggregated contributions (data, patterns, RL feedback).

### 6. Agent-to-Agent Collaboration

Agents on different nodes interact via A2A tasks. All cross-agent actions flow
through RACI approval. The human who owns an agent controls its trust boundary —
what it can do autonomously vs what requires approval when interacting with other
agents.

### 7. Presence in Human Spaces

When agents participate in human-interactive contexts (chat rooms, dashboards,
notification channels):

- Agents are visually disambiguated: "Admin's TIDY" not just "TIDY"
- The formal identity is always available on hover/inspect
- Agents declare their capabilities and limitations (via Agent Card)
- Human moderators can grant/revoke agent presence in any channel

### 8. Federation Topology — Mesh with Elected Coordinators

Axiom federations use a **mesh topology by default** — all nodes are peers.
When the federation exceeds a configurable threshold (default: 10 nodes), nodes
elect a **coordinator** to handle resource routing, membership lists, and health
aggregation. The coordinator is an optimization, not a requirement — if it fails,
the mesh continues operating in pure peer-to-peer mode.

**Rationale:** Pure mesh scales poorly past ~10 nodes for resource discovery
(O(n²) gossip). A star topology creates a single point of failure. Mesh with
elected coordinators gives O(1) resource lookups through the coordinator while
maintaining mesh resilience as fallback. This matches axiom's "self-sufficient
by default" principle — federation governance is additive.

### 9. Trust Bootstrap — Invitation-Based

New nodes join a federation via invitation:

1. An existing federation member generates a one-time invite token (`axi federation invite`)
2. The token is shared out-of-band (email, chat, QR code)
3. The new node presents the token (`axi federation join <token>`)
4. Both the inviting member's human and the joining node's human approve
5. Key exchange completes; the new node is federated

Tokens expire after a configurable TTL (default: 24 hours). No auto-join, no
implicit trust. Every federation membership requires mutual human approval.

**Rationale:** Organization SSO requires centralized identity infrastructure
that many deployments (field laptops, lab clusters) won't have. Manual mTLS
exchange is too high-friction for routine use. Invitation-based trust mirrors
patterns users already understand (Tailscale, Slack invites, WireGuard) while
preserving the security guarantee that both parties explicitly consent.

### 10. The Atomic Unit — What Is an Axiom Node?

An axiom node is any device running the **core runtime** — a minimal set of
components sufficient to operate independently and participate in a federation:

- **Identity manager** — Ed25519 keypair, agent identity, A2A Agent Card
- **A2A server** — HTTP endpoint for agent discovery and communication
- **Config store** — local configuration and state
- **Extension loader** — loads optional capabilities (agents, domain logic, federation)
- **Local gateway** — LLM routing (even if only to a tiny local model)

Nodes are classified into **profiles** based on what they provide:

| Profile | Description | Federation Role |
|---------|-------------|----------------|
| **Leaf** | Minimal core runtime, consumes shared resources | Consumer only |
| **Standard** | Core + local LLM + local RAG, self-sufficient | Consumer + contributor |
| **Provider** | Standard + shares resources to federation | Resource host |
| **Coordinator** | Provider + elected coordination role | Routing + governance |

Every profile includes the core runtime. A leaf node on a disconnected laptop
is still a complete axiom installation. Federation and domain capabilities are
extensions — they enhance but never become prerequisites.

See `prd-federation.md` §3 and `spec-federation.md` §2 for full detail.

### 11. Ecosystem Interoperability

Axiom must coexist with the broader agent ecosystem. As of early 2026, MCP
has universal adoption (9/9 major frameworks) and A2A has strong adoption
(5/9). Axiom's interoperability strategy is layered:

| Priority | Protocol | Reach | Purpose |
|----------|----------|-------|---------|
| P1 | MCP server (Streamable HTTP) | All frameworks | Expose axiom capabilities as tools |
| P2 | A2A Agent Card | CrewAI, LangGraph, MS Agent Framework, Google ADK, Bedrock | Agent discovery and task delegation |
| P3 | AG-UI (future) | MS Agent Framework, Bedrock | Frontend streaming (when needed) |

External agents interact with axiom through the same RACI gates as internal
agents. An external CrewAI agent calling an axiom MCP tool is subject to the
same authorization checks as a local TIDY agent. No framework gets implicit trust.

See `prd-federation.md` §9 and `spec-federation.md` §8 for integration details.

## Resolved Questions

These questions were open in the initial proposal and have been resolved:

| # | Question | Resolution |
|---|----------|-----------|
| 1 | A2A version to target | Target A2A v0.3 behind an adapter layer. Migration to v1.0 is a config change, not a rewrite. |
| 2 | Federation topology | Mesh with elected coordinators (Decision §8 above). |
| 3 | Conflict resolution | Three-tier: (a) auto-merge for non-contradictory state, (b) coordinator arbitration for operational conflicts, (c) human escalation for safety-critical or irresolvable disagreements. |
| 4 | Bandwidth/latency | SLA targets: heartbeat 10s, discovery <30s, task creation <2s LAN / <5s WAN, knowledge sync <5min. Nodes on metered connections can set bandwidth caps. |
| 5 | Offline federation | Nodes operate fully offline with local resources. On reconnect: version-vector sync for config, spec-rag-community merge for knowledge, queue replay for pending A2A tasks. |
| 6 | Trust bootstrap | Invitation-based with mutual human approval (Decision §9 above). |
| 7 | Agent versioning | Agents interoperate if they share the same major version of the A2A protocol adapter. Agent Cards declare supported protocol versions; incompatible agents get a clear error, not silent failure. |

## Consequences

- Axiom nodes form a self-organizing network when on the same LAN/VPN
- Shared resources are adopted automatically as they come online
- Individual nodes remain fully functional when disconnected
- Agent identity is unambiguous across the federation
- Organizational memory emerges from federated knowledge aggregation
- External agent frameworks can discover and interact with axiom via MCP and A2A
- The whole becomes greater than the sum of the parts

## Related Documents

- `prd-federation.md` — Full PRD for federation (companion to this ADR)
- `spec-federation.md` — Technical specification for federation protocols
- `spec-rag-community.md` — Federated knowledge aggregation (most mature piece)
- `adr-002-hyperledger-fabric-multi-facility.md` — Multi-facility audit
- `adr-006-mcp-agentic-access.md` — MCP for tool/context interface
- `adr-015-shared-service-boundaries.md` — Single-node service ownership
- `prd-managed-infrastructure.md` — Infrastructure provisioning and discovery
- `prd-agents.md` — Agent design, RACI, safety guardrails
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
