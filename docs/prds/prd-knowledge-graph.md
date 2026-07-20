# Product Requirements: Knowledge Graph Layer

**Product / Feature:** Knowledge Graph Layer (Axiom Core)

**Owner:** Ben Booth   •   **Status:** Draft   •   **Last updated:** 2026-04-07

**Related:** `spec-knowledge-graph.md`, `spec-rag-knowledge-maturity.md`, `spec-rag-architecture.md`, `spec-rag-community.md`, `prd-rag.md`, `prd-connections.md`

---

## 1) Elevator Pitch

A graph-based knowledge representation layer that extracts entities and relationships from validated RAG content, enabling structured navigation, cross-document reasoning, and federated knowledge merging — accessible from any coding tool or agent runtime.

## 2) Problem / Opportunity

- **RAG retrieval is stateless.** Vector similarity finds relevant chunks but cannot answer "what depends on what?" or "show me all entities related to X across 200 documents." Operators need structural reasoning over their corpus, not just keyword/semantic lookup.
- **Knowledge maturity stalls at Layer 2 (Facts).** The existing pipeline extracts propositions but stores them as isolated text+embedding rows. There is no way to connect facts into frameworks (Layer 3) without a relationship model.
- **Existing graph tools are IDE-coupled.** Tools like Graphify (6.8k stars, MIT) use JSON file storage and Claude Code hooks — useful for developers, but not scalable for multi-user federated deployments. No access tier model, no multi-tenancy, no federation.
- **External systems handle ontology, not operational knowledge.** An external ontology/engineering system can own domain ontology + lineage; Axiom owns operational intelligence and consumes that ontology (1:1 metatype mapping) via `OntologyProvider` when configured, and runs fully without it. The graph layer fills the gap between raw RAG chunks and formal ontology — interoperable with any conforming external ontology provider while maintaining independence.

## 3) Goals & Success Metrics

- **Primary goal:** Structured knowledge navigation and cross-document reasoning on top of the existing RAG corpus and knowledge maturity pipeline.
- Success metrics:
  - Query answerable via graph traversal that RAG alone cannot answer (e.g., "what procedures reference valve V-101?") — 10+ use cases documented
  - Graph populated automatically from validated Layer 2 facts — no manual entity tagging
  - Sub-2s graph query latency for 2-hop traversals on 100k-node graphs
  - Works from any client: CLI (`axi graph`), HTTP API, MCP server, a consumer-extension Chat agent

## 4) Key Users / Personas

- **Facility operator (an external researcher):** Asks structured questions ("what components are affected by this procedure change?"). Expects grounded answers citing source documents. Uses `axi chat` or a consumer-extension Chat.
- **Researcher (domain researchers):** Explores relationships between simulation codes, material properties, and experimental results. May query the graph directly or through agent-mediated RAG.
- **Agent runtime (SCAN, TIDY):** Uses graph traversal to inform knowledge maturity promotion (Layer 2→3), contradiction detection, and federation merge decisions.
- **External tool user:** Accesses graph via MCP server from VS Code, Cursor, Windsurf, or any MCP-compatible coding tool.

## 5) Scope — Key Capabilities (MVP)

1. **Entity/relationship extraction from validated chunks** — Automated pipeline extracts entities (components, procedures, documents, people, codes, materials) and typed relationships from Layer 0-2 content. Deterministic AST extraction for code; LLM-assisted extraction for prose. Confidence scores on all inferred edges.
2. **Graph storage in PostgreSQL via Apache AGE** — Cypher query language on existing PG infrastructure. No new database. Respects access tier (public/restricted/export_controlled) and scope (community/facility/personal) on every node and edge.
3. **Graph query API** — `POST /api/v1/graph/query` accepts Cypher or natural language. Returns subgraphs, paths, or flattened results. Authenticated via federation node identity (same as RAG endpoint).
4. **CLI integration** — `axi graph query "..."`, `axi graph status`, `axi graph rebuild`. Domain-agnostic; no domain-specific entity types in Axiom core.
5. **RAG+Graph hybrid retrieval** — When `axi chat` detects a structural query (entity relationships, dependency chains, "what references X?"), fan out to both RAG vector search and graph traversal, merge results.
6. **Federation-aware graph merging** — Community-scope graph nodes/edges sync between federated peers using the same trust gradient (GREEN/YELLOW/RED) as knowledge facts. Graph merge uses entity resolution (fuzzy matching + embedding similarity) to align nodes across facilities.
7. **MCP server endpoint** — Expose graph query as an MCP tool so any MCP-compatible coding assistant can traverse the knowledge graph.
8. **External ontology integration (OntologyProvider)** — Consume domain entity/relationship types from an external ontology system via REST/MCP (any conforming provider). 1:1 mapping: external metatype → EntityType, metatype relationship → RelationshipType. Fully optional — system operates with core types + extension hardcoded fallbacks when no provider is configured. Cached locally for offline use.

## 6) Non-Functional / Constraints

- **No new database.** Apache AGE runs as a PostgreSQL extension alongside pgvector. Single PG instance serves RAG, facts, and graph.
- **Access tier enforcement.** Graph queries MUST filter by caller's access tier. A public-tier query never returns restricted or EC nodes/edges. Tier is inherited from source chunk/document.
- **Domain-agnostic.** Axiom core defines generic entity types (Document, Component, Procedure, Person, Code, Material, Concept). Domain extensions (e.g. a nuclear-engineering consumer) add domain-specific types (Reactor, FuelElement, Isotope, etc.).
- **Tool-agnostic.** The graph layer MUST be usable from: Axiom CLI, HTTP API, MCP server, a consumer-extension Chat, and any future agent runtime. No IDE-specific hooks or platform coupling.
- **Incremental build.** Graph extraction runs incrementally on new/changed documents, not full rebuilds. Content-hash dedup prevents re-extracting unchanged content.
- **Performance.** 2-hop Cypher queries < 2s on 100k nodes. Full graph rebuild of 10k documents < 30 minutes.

## 7) Timeline (high level)

- **Phase 0.1:** Schema + extraction pipeline + CLI — targeting P3 (post-external-researcher release)
- **Phase 0.2:** RAG+Graph hybrid retrieval + MCP server — P3/P4
- **Phase 1.0:** Federation graph merging + community graph packs — P4+

## 8) Risks & Open Questions

- **Apache AGE maturity.** AGE is incubating at Apache. Risk: query planner edge cases, extension compatibility with PG 16. Mitigation: evaluate AGE 1.5+ on a self-hosted node's PG before committing; fallback to plain PG tables with recursive CTEs if AGE is unstable.
- **Entity resolution across facilities.** Fuzzy matching + embedding similarity may produce false merges. Mitigation: confidence threshold on entity resolution; YELLOW/RED path for ambiguous merges.
- **Extraction quality.** LLM-based entity extraction from technical PDFs (equations, tables) may be noisy. Mitigation: deterministic extraction first (headings, cross-references, code AST), LLM only for prose; human review on RED path.
- **Open question:** Should the graph layer own its own embedding column or reuse chunk embeddings from the RAG store? (Decide by Phase 0.1 design review.)
- ~~**Open question:** How does the graph layer interact with an external ontology?~~ **Resolved.** An external ontology provider is an optional ontology source. 1:1 mapping: external metatype → Axiom EntityType, external metatype relationship → Axiom RelationshipType. Axiom runs fully without one (hardcoded fallback + offline cache). Bidirectional sync (Axiom→provider entity push) deferred to Phase 1.0. See `spec-knowledge-graph.md` §8.

## 9) Acceptance & Rollout

- Who signs off: Ben Booth (product + eng lead)
- Rollout plan:
  - Phase 0.1: Internal testing on a self-hosted node with restricted corpus
  - Phase 0.2: Canary deployment to an external researcher's node
  - 1.0: Federation-wide via community graph packs
- Rollback: Graph layer is additive — disable extraction pipeline and graph query routing without affecting existing RAG functionality.

## 10) Contacts & Links

- Product + eng lead: Ben Booth (no-reply@axiom-os.ai)
- Tech spec: `docs/specs/spec-knowledge-graph.md`
- Knowledge maturity spec: `docs/specs/spec-rag-knowledge-maturity.md`
- RAG architecture spec: `docs/specs/spec-rag-architecture.md`
- Inspiration: [Graphify](https://github.com/safishamsi/graphify) (MIT, concept validation)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
