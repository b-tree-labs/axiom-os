# ADR-037: Federation State Propagation — Signed Gossip, Not Blockchain

**Status:** Proposed (2026-04-29)
**Supersedes:** none (extends ADR-022 federation identity, ADR-024 root availability + delegation, ADR-027 federated memory, ADR-028 trust graph; complements ADR-036 surfaces + slots)
**Related:** ADR-016 (multi-node federation), ADR-022 (identity & membership), ADR-023 (topology lifecycle), ADR-024 (root availability + revocation channel), ADR-027 (federated memory), ADR-028 (trust graph), ADR-036 (runtime surfaces + install slots), `prd-canary-nodes` + `spec-canary-nodes` (attestation distribution sinks), `prd-federation §17` (16 install/upgrade scenarios), `feedback_agent_liveness_peer_observable.md` (the 2026-04-29 lesson that motivated the buddy-detection requirement).

## Context

Today Axiom federation state is fragmented across several ad-hoc surfaces: a cohort registry (ADR-022), the trust graph (ADR-028), canary attestation sinks (`spec-canary-nodes` §4), the ownership model (ADR-026), and per-node state in `~/.axi/`. There is no single primitive any node can query to answer:

- "Who am I, what cohorts am I in, what node am I peered with?"
- "Is example-host.example.org healthy right now? When did its TIDY last successfully tick?"
- "What does the organizational tree look like from my position?"
- "Has anyone in this cohort already installed Axiom? What spec are they running?"
- "Who else is reachable from here, and what can I see about them?"

Each of these is currently answered by a different mechanism (or not at all). The 2026-04-29 self-hosted-node incident (`feedback_agent_liveness_peer_observable.md`) made the gap concrete: TIDY failed to launch on every tick for 14 days and nothing detected it. *A friend would have noticed.* The friend doesn't exist today, because the federation has no shared notion of "what is the current liveness of any agent on any peer node."

The leader-directed onboarding work (ADR-038, in flight) needs this same primitive — a leader inviting students must first query the federation: "is `student@school` already a node? if so, what spec are they running?" Without a queryable state primitive, leader onboarding has to invent one.

So we have three independent forces converging on the need for a unified federation state primitive:
1. Operational observability (the buddy-detection requirement).
2. Leader-directed onboarding (search-then-invite).
3. UI / visualization across CLI, chat, and (post-Prague) web surfaces — operators want to *see* their federation, not just take its existence on faith.

The question is not whether to build a federation state primitive. The question is what shape it takes.

## Decision

Axiom federation state is a **signed, gossip-replicated, DNS-style directory of typed records** — explicitly *not* a blockchain. Each node is the authority for its own records; cohort roots are authority for membership records. Peers maintain a local view of what they've been told; views are eventually consistent and may diverge in detail; queries hit the local view first.

### D1 — Gossip + DNS-style, not blockchain

The blockchain shape solves a different problem: Byzantine-fault-tolerant agreement on a single global truth among adversarial peers who don't trust each other. That's the wrong problem for Axiom federation:

- Federation is *operationally cooperative* within cohorts. A research program, an instructor's classroom, an inter-institutional alliance — these are not adversarial settings. The trust model is asymmetric: cohort members trust the cohort root; the root has revocation power; outsiders are quarantined.
- The actual workload is *local-view queries* ("show me my view"), not *global-truth establishment* ("what is THE federation state"). The DNS analogy holds: anyone can query DNS, no one needs global agreement on a Merkle root for that to work.
- Consensus latency is wrong for the use cases. "Is this node healthy *right now*" is a sub-second query; blockchain finality is minutes-to-hours.
- The operational surface area of blockchain (key ceremonies, fork resolution, network partitions, validator pools) is a permanent tax that Axiom federation does not need to pay.

The gossip + DNS-style shape:
- Each node publishes signed records into a local store.
- Peers gossip records on a periodic cadence + on relevant events (join, revocation, classification changes).
- Each peer maintains a *local view* — its own records plus what it's heard about visible peers. Views may diverge in detail; that's fine.
- Queries always answer from the local view first. If a query needs fresher data, it can request a peer-push from a known-authoritative source.
- Records have TTLs and signatures. Stale records are observable as such (they carry their last-update timestamp).

### D2 — Discovery and state propagation are the same primitive

Today, `mDNS` LAN discovery (`prd-federation §17.1 #16`) and the cohort registry (ADR-022) are separate mechanisms. They are not separate concerns — both answer "who is out there?" The unified federation directory subsumes both:

- "Discover peers on my LAN" = query the local view filtered by `transport=mdns AND first_seen > now - 1h`.
- "Who is in cohort X?" = query the local view filtered by `cohort_membership ⊃ {X}`.
- "Where is `node_id=...`?" = query the local view for that node's `transport_address` record.

mDNS becomes a *gossip transport* (one of several) rather than a separate discovery mechanism. The cohort registry becomes a *record type* in the directory rather than a separate registry. Discovery and state are two views of the same underlying primitive.

### D3 — Records are typed; the type set is open and versioned

Records are typed similarly to DNS resource records. Each record type has a defined schema:

| Type | Authority | Carries | TTL guidance |
|---|---|---|---|
| `IDENTITY` | self (signed by node's key) | `node_id`, `pubkey`, `slot_id`, surface, surface_evidence (per ADR-036 §D6) | days |
| `TRANSPORT` | self | reachable address(es), protocol, port, mDNS hint | minutes |
| `COHORT_MEMBERSHIP` | cohort root | `(cohort_id, node_id)` pair, signed by root | days |
| `CAPABILITY` | self | extension manifest summaries (name, version, kind), declared surface support | hours |
| `LIVENESS` | self (and observed by peers) | per-agent last-successful-heartbeat timestamps; sibling-observed liveness with observer signature | minutes |
| `RELATIONSHIP` | self | declared peer relationships (who this node treats as peer, leader, follower) | hours |
| `CLASSIFICATION` | self | what classification level(s) this node operates at (per spec-classification-boundary) | days |
| `REVOCATION` | cohort root, then gossipped per ADR-024 | quarantine / revocation events; supersedes any other record from the revoked node | indefinite |

The type registry is open: extensions may register new record types via their manifest (Phase 2/3). Type schemas are versioned; consumers must handle unknown types gracefully (record is still cached and gossipped, just not interpreted).

### D4 — Authority is per-record-type, not per-record

The authority for a record is determined by the record type, not by who happens to be holding the record. A node's `IDENTITY` record is authoritative if and only if it is signed by the node's own key. A `COHORT_MEMBERSHIP` record is authoritative if and only if it is signed by the cohort root's key. A `LIVENESS` record's *self* claim is authoritative for the node's own claim ("I think I'm healthy"); the *observed* claim is authoritative only for the observer's view ("example-host observed tidy silent").

Conflict resolution:
- Within a record type from the same authority: last-writer-wins by `signed_at` timestamp.
- Across authorities for the same record type: not permitted; schema rejects.
- A `REVOCATION` record from the cohort root supersedes all other records from the revoked subject.

### D5 — Liveness is a record type; buddy detection is a query

The 2026-04-29 lesson (`feedback_agent_liveness_peer_observable.md`) is operationalized here:

- Each agent publishes a `LIVENESS` record on every successful heartbeat: `(node_id, agent_name, last_successful_ts, expected_interval_secs)`.
- Sibling agents on the same node also publish *observed-liveness* records: `(observer_node_id, observer_agent, observed_node_id, observed_agent, last_observed_ts, observation_method)`.
- Federation peers gossip both kinds.
- Querying `LIVENESS` for any visible node returns the freshest record. **Stale-by-more-than-N-intervals is itself the failure signal**, regardless of what the record content claims.

This means a node can ask: "show me all visible agents whose `LIVENESS` is older than 2× their declared `expected_interval_secs`" — and that query answers "who's silent?" deterministically, without any agent having to actively report unhealth.

The same primitive serves the buddy-detection use case (peer notices my agent is silent), the leader's spec-compliance check (is the follower's RIVET heartbeat current?), and the operator's dashboard (TIDY on example-host hasn't ticked in 4 hours — yellow alert).

### D6 — Visibility is policy-bounded

A peer does not see the entire directory. Visibility is determined by cohort membership intersected with classification policy:

- Records within a cohort are visible to all cohort members by default.
- Cross-cohort visibility requires an explicit federation relationship (per ADR-027 federated memory propagation rules).
- Classification gates per `spec-classification-boundary.md` apply: a peer at classification level L sees only records authorized for L (or below, depending on cohort policy).
- The federation gateway redacts InstallContext correlation handles per ADR-036 §D6 when records cross cohort boundaries.

The directory is *not* a single global namespace. Each peer's local view is the view it is authorized to maintain.

### D7 — Gossip topology emerges from cohort membership

Phase 0/1 keeps the gossip topology simple: each peer gossips with the other members of each cohort it belongs to. Cohort root peers act as natural rendezvous points. Anti-entropy runs on a cadence (default 30s for fast-changing types like LIVENESS, 5min for slow-changing like CAPABILITY).

Phase 2/3 may layer optimizations: gossip fanout limits, vector-clock anti-entropy, push-only updates from authority for high-frequency types. None of these are Phase 0 concerns.

### D8 — UI surfaces render the same underlying directory

The directory exposes a single query API. Every UI surface renders the same query results in surface-appropriate ways:

- **CLI:** `axi federation peers`, `axi federation tree`, `axi federation status`, `axi federation lookup <node>`, `axi federation watch` (live tail of changes). Defaults to the operator's local view; flags to expand or filter.
- **Chat (AXI):** natural-language access. "Who's in my cohort?" → "You're in `ne101-prague-2026` with 12 students, 1 instructor, and 2 TAs. All currently healthy except `student-7@charles` whose Tidy has been silent for 3 hours."
- **Browser / dashboard (post-Prague):** force-directed graph of the visible federation; org-tree view of cohort hierarchy; per-node detail card; live update on gossip.
- **IDE extension:** sidebar showing your immediate peers + their liveness; click-to-jump to lookup.

The contract: every surface is rendering the same query. If the data is wrong, it's wrong everywhere — which is the right property; we never want UI surfaces disagreeing because they consult different sources.

### D9 — The directory is local-first and survives partition

A peer's local view is durable on disk (under the slot's state dir). It persists across restarts, network partitions, and federation outages. Queries against the local view never depend on network availability. The federation gateway is *one source* of updates, not the only source.

This is the operational property that matters when an instructor is teaching a class with intermittent connectivity, when a research site is briefly air-gapped, or when a federation root is undergoing maintenance. The directory degrades to "what I last knew" rather than "I can't answer anything."

## Consequences

**Positive:**
- Single primitive answers discovery, state, observability, and onboarding-search queries.
- Buddy detection becomes a query against existing data, not a new mechanism.
- Leader-directed onboarding (ADR-038) inherits a queryable federation directory rather than inventing one.
- UI surface integration is well-defined; surfaces don't disagree about facts.
- Operationally simple: no consensus protocol to debug, no validator pool to operate, no fork resolution.
- mDNS, cohort registry, attestation distribution become *transports / record types* in a unified primitive rather than separate concerns.
- Local-first behavior survives partitions cleanly.

**Negative / costs:**
- Eventual consistency means two peers may briefly disagree about a record's freshness. For most queries this is invisible; for adversarial use cases (a malicious peer claiming a stale record is current) signatures + timestamps + cohort-root authority disambiguate.
- Gossip bandwidth scales with cohort size. At 10k–100k nodes per `project_federation_scale_target`, naive all-pairs gossip is too expensive. Phase 2/3 fanout-limit + structured topology required; Phase 0/1 ships unbounded for small cohorts.
- A new on-disk format (the local view database) joins MemoryFragment storage, ownership store, and trust graph as a Tier-0 schema concern. Additive, but real.
- Record-type registry needs governance — anyone can add a type but the well-known types should not collide. ADR-031 extension self-containment helps; record-type names should be namespaced.

**What this ADR does NOT do:**
- Does not replace MemoryFragment provenance or the trust graph. The directory is a *position and relationship* primitive; provenance and trust are *content* primitives. They reference each other (a `LIVENESS` record may cite a `MemoryFragment` source), but they remain separate stores.
- Does not specify the on-the-wire gossip protocol in detail. That belongs to the tech spec.
- Does not solve cross-host forked-identity (ADR-036 D4 follow-on). The directory makes detection *easier* (peer-side `(node_id, slot_id, transport_key)` tuples are now in scope as record types) but the detection ADR is separate.

## Threat model

| Threat | Closed by |
|---|---|
| Malicious peer publishing a fake `LIVENESS` record for another node | Authority is per-record-type; LIVENESS-self is signed by the node's own key; observed-LIVENESS carries observer's signature. Peers reject unsigned or wrong-authority records. |
| Stale record presented as current | All records carry `signed_at`; stale records are observable; consumers gate on freshness, not on presence. |
| Record poisoning via gossip flooding | Per-record TTL + rate-limited per-source ingestion + record-type-specific size limits. Phase 2/3 may add signed acknowledgement; Phase 0 relies on cohort-root revocation power. |
| Cross-cohort information leak via gossip | D6 visibility policy + federation gateway redaction (ADR-036 §D6). Records crossing cohort boundaries are projected, not propagated raw. |
| Quarantined node continues publishing records | REVOCATION records from cohort root supersede all other records from the revoked subject. Peers verify the supersession on every read. |
| Eclipse attack: malicious peers isolate a target by controlling its gossip neighborhood | Phase 0/1 trusts cohort topology — small cohorts make eclipse hard. Phase 2/3: structured gossip with diversity requirements. Tracked as follow-on. |

Threats acknowledged but not closed by this ADR:

| Threat | Why deferred |
|---|---|
| Eclipse attack at scale | Phase 2/3 work; small cohorts (Prague-class) don't need it yet. |
| Sybil attack on cohort membership | Cohort root is the membership authority; Sybil attacks reduce to "compromise the root" which is the existing ADR-024 threat. The directory doesn't introduce new Sybil vectors. |
| Side-channel inference from query patterns | A peer querying `LIVENESS` for a specific node leaks "I'm interested in this node." Not addressed; tracked as a privacy concern for classified contexts. |
| Cross-host forked-identity detection | Tracked as follow-on ADR per ADR-036 §D4 limitation. The directory provides the *building blocks* (gossip of `(node_id, slot_id, transport_key)`) but the detection ADR formalizes the algorithm. |

## Compliance gates introduced

- `pytest -m federation_directory_compliance` (new marker):
  - Every record type has a defined schema and a signature requirement.
  - Authority verification rejects unsigned and wrong-authority records.
  - Visibility policy enforcement: a peer at classification L cannot see records authorized only above L.
  - REVOCATION records correctly supersede all other records from the revoked subject.
  - Local view persists across process restart.
  - Stale-record detection: records older than `expected_interval_secs * 2` are flagged in queries.
  - mDNS LAN discovery exposes results as `TRANSPORT` records in the directory.
  - Cohort membership query returns the same set as the legacy cohort-registry query (back-compat during migration).

These join `accountability_compliance` (ADR-035), `pipeline_compliance` (ADR-034), and `surface_compliance` (ADR-036) as release gates.

## Phasing

- **Phase 0 (this PR):** This ADR + PRD + tech spec. No code yet.
- **Phase 1:** Local view database, record type registry, IDENTITY + TRANSPORT + LIVENESS record types. CLI surfaces (`axi federation peers`, `axi federation status`). Buddy-detection query lights up. mDNS subsumed as a transport. Cohort registry queries route through the directory but the legacy cohort registry remains the authority-of-record (additive overlay).
- **Phase 2:** COHORT_MEMBERSHIP + CAPABILITY + RELATIONSHIP + CLASSIFICATION record types. Cohort registry migrates to be a *consumer* of the directory rather than a separate authority. Chat / AXI surface renders federation state in natural language. Drift detection between legacy registry and directory while the migration is in flight.
- **Phase 3 (post-Prague):** Browser / web dashboard renders the directory as graph + tree views. Gossip fanout optimization. Eclipse-resistance work. Cross-host forked-identity detection ADR builds on the directory.
- **Phase 4:** Extension-defined record types; record-type marketplace.

Phase 0/1 ships everything needed for buddy detection and for ADR-038 (leader-directed onboarding) to work. Phase 2 makes the directory the canonical source of federation state. Phase 3 is the visualization payoff. Phase 4 opens it to extensions.

## Open items

- **Wire format and library selection.** The PRD/spec evaluates options: CRDT libraries (Automerge, Yjs), purpose-built (custom), or wrapping an existing peer-to-peer state library (libp2p / Hyperswarm). Phase 0 doesn't pick; Phase 1 must.
- **Local view storage backend.** SQLite is the natural choice given Tier-0 fallback (`project_tier0_infra`); the PRD evaluates alternatives.
- **Record TTL governance.** Who can specify TTL: the record type schema, the publisher, both? The PRD proposes a per-type default with publisher-tightenable bounds.
- **Visualization defaults.** What does "your view" show by default in CLI and chat? Just immediate peers? Two hops? Whole cohort? The PRD proposes "immediate cohort" with explicit expand commands; UX details belong to the spec.
- **Migration of existing federation state.** Cohort registry, trust graph, attestation sinks all become *consumers / record types* eventually. Migration sequencing matters; the PRD lays it out per phase.
- **Privacy in classified contexts.** Query-pattern side-channels (D6 footnote) need a separate spec amendment if any classified deployment has adversarial-observer threat in scope.

## The bottom line

A friend would have noticed TIDY. The federation should be that friend, by construction — not because we ask each agent to monitor every other agent, but because each agent publishes "I am here, I am alive" into a primitive any peer can query. The same primitive answers leader-directed onboarding's "is this human already a node?" and the operator's "show me my federation tree" — three apparently-separate features collapse onto one mechanism that is *small*, *operationally simple*, and *DNS-shaped* rather than blockchain-shaped. The cost is one new on-disk format and a gossip protocol; the benefit is that several future features stop having to invent their own state primitive.

_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
