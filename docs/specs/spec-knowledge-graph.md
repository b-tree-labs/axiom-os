# Knowledge Graph Layer

**Status:** Draft (subordinate to `spec-memory.md` as of 2026-04-26)
**Owner:** Ben Booth
**Created:** 2026-04-07
**Last Updated:** 2026-04-26
**Layer:** Axiom core — backend implementation for `spec-memory.md §5` (Layer 2 ConceptGraph)
**Authority:** This spec is **subordinate to `spec-memory.md`**. On any conflict between this spec and spec-memory, spec-memory wins. The Apache-AGE-on-Postgres backend specified here is **one storage implementation** behind the Axiom-owned `ConceptGraph` protocol; the protocol, semantics, replay invariants, and provenance contract live in spec-memory §5.
**Related:** `spec-memory.md` (authoritative for graph semantics + ConceptExtractor protocol), `spec-rag-knowledge-maturity.md`, `spec-rag-architecture.md`, `spec-rag-community.md`, `spec-federation.md`, `prd-knowledge-graph.md`, `prd-connections.md`, `prd-memory.md`

---

## 0. Reconciliation with spec-memory.md (added 2026-04-26)

`spec-memory.md` introduces a Layer 2 `ConceptGraph` protocol with formal `Concept`, `ConceptEdge`, and `ConceptExtractor` types, plus invariants for replay (I8), versioning (I9), provenance (I10), classification-aware extractor selection (I11), conflict resolution (I12), and async-capable extraction (I13).

This spec — written before that one — used different terminology (`entity` / `relationship` / `extraction_pipeline`) and a different storage substrate (Apache AGE on Postgres). The reconciliation:

- The **terminology in spec-memory wins** for all new code: `Concept` (not `entity`), `ConceptEdge` (not `relationship`), `ConceptExtractor` (not `extraction_pipeline`). Where extension authors encounter both, spec-memory is canonical.
- The **Apache AGE backend specified here is the recommended Server-tier** `ConceptGraph` implementation per `spec-memory §5.4`. It is one option behind the protocol; the SQLite-backed default (Edge / Workstation profiles) is shipped as the primary v0.
- The **extraction pipeline specified here registers as `ConceptExtractor`s** per spec-memory §5.3, complete with `ExtractorCapability` declarations (data-flow, classification gate). RAG-chunk entities and `MemoryFragment`-derived concepts share the canonical `concept_id` namespace.
- The **MemoryFragment provenance contract** (T,U,A,R) is upstream of every concept; this spec's "provenance" notion is subsumed.

When the AGE backend implementation lands behind the protocol, this spec's §3 (Extraction Pipeline) and §4 (Query Interface) get re-anchored as backend-specific notes. Until then, treat this spec as **describing one realization**; spec-memory describes the surface every consumer sees.

---

## Terms Used

| Term | Definition | Reference |
|------|-----------|-----------|
| `entity` | A discrete named concept extracted from content: component, procedure, document, person, code, material, or concept | `axiom glossary entity` |
| `relationship` | A typed, directed edge between two entities with provenance and confidence | `axiom glossary relationship` |
| `knowledge_graph` | The set of entities, relationships, and their metadata stored in Apache AGE on PostgreSQL | `axiom glossary knowledge_graph` |
| `entity_resolution` | The process of determining whether two entity references across documents or facilities refer to the same real-world thing | `axiom glossary entity_resolution` |
| `extraction_pipeline` | The automated process that produces entities and relationships from RAG chunks and knowledge facts | `axiom glossary extraction_pipeline` |
| `graph_pack` | A versioned, portable subgraph distributed as part of a domain pack (`.axiompack`) | `axiom glossary graph_pack` |

All terms resolve via `axiom glossary <term>` or `docs/glossary-axiom.toml`.

---

## 1. Overview

The RAG store answers "what chunks are semantically similar to this query?" The knowledge maturity pipeline answers "what validated facts have been extracted from interactions?" The knowledge graph answers a third class of question: **"how are things related?"**

Structural queries — "what procedures reference valve V-101?", "what simulation codes were used to validate this material property?", "show me the dependency chain from this regulatory requirement to facility procedures" — require explicit entity-relationship representation. Embedding similarity cannot reliably recover these relationships because structural proximity and semantic proximity are different axes.

The knowledge graph layer sits alongside (not above or below) the RAG store and knowledge facts table. It is populated from the same content but stores a different projection of it: **entities and typed relationships** rather than text chunks or propositions.

```
                    ┌─────────────────────────────┐
                    │       Query Router           │
                    │  (structural? → graph)       │
                    │  (semantic?   → RAG)         │
                    │  (hybrid?     → both+merge)  │
                    └──────┬──────────┬────────────┘
                           │          │
                    ┌──────▼──┐  ┌────▼──────────┐
                    │ Graph   │  │ RAG Store      │
                    │ (AGE)   │  │ (pgvector)     │
                    └──────┬──┘  └────┬───────────┘
                           │          │
                    ┌──────▼──────────▼────────────┐
                    │     PostgreSQL 16             │
                    │  AGE + pgvector + pg_trgm     │
                    └──────────────────────────────┘
```

### Design Principles

1. **No new database.** Apache AGE is a PG extension. The graph lives in the same PG instance as the RAG store. One connection string, one backup, one ops surface.
2. **Extraction from validated content, not raw docs.** Entity extraction runs on RAG chunks (Layer 0) and knowledge facts (Layer 2). Raw file bytes never touch the graph pipeline.
3. **Deterministic first, LLM second.** Code is parsed via tree-sitter AST (no LLM needed). Document cross-references and heading structure are extracted deterministically. LLM extraction is reserved for prose relationships where no deterministic signal exists.
4. **Access tier on every node and edge.** A graph traversal query is filtered by the caller's maximum allowed tier before execution. Restricted nodes are invisible to public-tier queries. Export-controlled nodes exist only on PrivateCloud graphs.
5. **Tool-agnostic API.** The graph is queryable via: Cypher (direct), HTTP API, MCP tool, CLI (`axi graph`), and agent function call. No IDE-specific coupling.
6. **Domain-agnostic core.** Axiom defines generic entity types. Domain extensions register additional types at runtime.

---

## 2. Storage: Apache AGE on PostgreSQL

### 2.1 Why AGE

| Requirement | AGE | Alternatives |
|---|---|---|
| No new database | PG extension — same instance as RAG | Neo4j = separate DB + license; NetworkX = in-memory only |
| Cypher query language | Native Cypher support | Plain PG = recursive CTEs (verbose, slow for >3 hops) |
| Access control | PG row-level security | Neo4j = separate RBAC model |
| Backup/restore | `pg_dump` captures everything | Neo4j = separate backup tooling |
| Federation | Same PG replication tooling | Neo4j = separate clustering |
| License | Apache 2.0 | Neo4j Community = GPL; Enterprise = commercial |

### 2.2 Installation

AGE is installed as a PG extension alongside pgvector:

```sql
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
```

Added to the `RAGStore.connect()` migration path (same pattern as the `content_hash` column migration).

### 2.3 Graph Schema

One graph per corpus scope:

```sql
-- One graph per scope (not per tier — tier is a node property)
SELECT create_graph('axiom_community');
SELECT create_graph('axiom_facility');
SELECT create_graph('axiom_personal');
```

### 2.4 Core Entity Types (Axiom Generic)

| Label | Description | Key Properties |
|---|---|---|
| `Document` | An indexed document in the RAG store | `source_path`, `title`, `corpus`, `access_tier`, `content_hash` |
| `Component` | A physical or logical component referenced in content | `name`, `aliases[]`, `access_tier` |
| `Procedure` | An operational procedure, standard, or regulation | `name`, `doc_number`, `revision`, `access_tier` |
| `Person` | A named individual (author, operator, reviewer) | `name`, `role`, `organization` |
| `Code` | A simulation code, software tool, or library | `name`, `version`, `language`, `repository` |
| `Material` | A physical material or substance | `name`, `formula`, `access_tier` |
| `Concept` | An abstract concept, theory, or methodology | `name`, `domain_tags[]` |
| `Fact` | A validated knowledge fact from the maturity pipeline | `fact_id`, `proposition`, `confidence`, `maturity_layer` |

Domain extensions (e.g., a nuclear-engineering consumer) register additional labels at runtime:

```python
# A domain consumer registers domain-specific entity types
graph.register_entity_types([
    EntityType("Reactor", parent="Component", properties=["type", "thermal_power_mw"]),
    EntityType("FuelElement", parent="Component", properties=["enrichment", "geometry"]),
    EntityType("Isotope", parent="Material", properties=["z", "a", "half_life"]),
    EntityType("Regulation", parent="Procedure", properties=["cfr_part", "section"]),
])
```

### 2.5 Core Relationship Types

| Type | From | To | Properties |
|---|---|---|---|
| `REFERENCES` | Document | Document | `section`, `context` |
| `DESCRIBES` | Document | Component | `section`, `context` |
| `GOVERNS` | Procedure | Component | `requirement_type` |
| `AUTHORED_BY` | Document | Person | `role` (author/reviewer/approver) |
| `USES` | Document | Code | `version`, `context` |
| `COMPOSED_OF` | Component | Material | `fraction`, `region` |
| `DEPENDS_ON` | Component | Component | `dependency_type` |
| `VALIDATES` | Fact | Document | `chunk_ids[]` |
| `CONTRADICTS` | Fact | Fact | `confidence`, `resolution_status` |
| `SUPERSEDES` | Document | Document | `effective_date` |

All edges carry:

```
{
  confidence: float,          -- 0.0-1.0, how certain the extraction is
  provenance: "extracted" | "inferred" | "human",
  source_chunk_id: int,       -- chunk that produced this edge
  access_tier: str,           -- inherited from max(source, target)
  extracted_at: timestamptz
}
```

---

## 3. Extraction Pipeline

### 3.0 Design Principle: Source Documents First

**Graph entities are extracted from source documents, not from RAG chunks.**

Chunking is lossy — it breaks tables, cross-references, section hierarchy. Extracting entities from 800-char fragments produces worse results than extracting from the full document with structure intact. The graph extraction pipeline operates on the **original document text** (full, unchunked), and its output (semantic boundaries, entity locations) then informs the RAG chunker.

This creates two parallel ingest paths from a single source:

```
Source Document (PDF, DOCX, MD)
    │
    ├──→ Graph Extraction (full doc, structure-preserving)
    │     → entities, relationships, section boundaries
    │     → semantic unit boundaries (tables, procedures, sections)
    │     → document_id anchor (content-hash, stable across re-ingest)
    │
    └──→ Semantic Chunker (informed by graph output)
          → chunks aligned to semantic units, not char counts
          → each chunk tagged with entity_ids[] and document_id
          → embeddings generated per chunk
          → stored in RAG pgvector store
```

Bidirectional linking: chunks reference graph entities (`chunk.entity_ids[]`), graph entities reference source documents (`entity.source_document_ids[]`), and knowledge facts reference documents (`fact.source_document_ids[]`). The `document_id` (content-hash-based) is the universal stable anchor — see `spec-rag-architecture.md` §12.

### 3.1 Architecture

Extraction is a four-stage pipeline that runs incrementally on new or changed documents. Stages 1-2 operate on the **full source document text**, not on chunks:

```
Stage 1: Deterministic Extraction (no LLM, full document)
  ├── Document cross-references (regex: NUREG-\d+, 10 CFR \d+, etc.)
  ├── Heading/section structure → section boundaries + implicit DESCRIBES edges
  ├── Table detection → table boundaries (one table = one semantic unit)
  ├── Procedure step detection → step boundaries
  ├── Code AST via tree-sitter → Code, function, class entities
  └── Metadata (author, date, revision) → Person, Document properties + citation metadata

Stage 2: LLM-Assisted Extraction (batched, confidence-scored, full document sections)
  ├── Entity recognition from document sections (not chunks)
  ├── Relationship extraction from document sections
  ├── Cross-reference resolution (link mentions to their targets)
  └── Confidence scoring on all inferred edges

Stage 3: Entity Resolution
  ├── Fuzzy name matching (Levenshtein + token overlap)
  ├── Embedding similarity (entity name + context → cosine > 0.90)
  └── Merge candidates above threshold; flag ambiguous for review

Stage 4: Semantic Boundary Export → RAG Chunker
  ├── Export section/table/procedure boundaries to chunker
  ├── Chunker aligns splits to semantic units (see spec-rag-architecture.md §12a)
  └── Each chunk tagged with entity_ids[] from Stages 1-3
```

### 3.2 Incremental Processing

The `content_hash` column on the `documents` table (added in v0.7+) drives incremental extraction:

```python
def extract_graph(store: RAGStore, graph: GraphStore, corpus: str) -> ExtractionStats:
    """Extract entities/relationships from documents not yet in the graph."""
    docs = store.get_documents_without_graph_extraction(corpus)
    for doc in docs:
        chunks = store.get_chunks_for_document(doc.source_path, corpus)
        # Stage 1: deterministic
        entities, edges = deterministic_extract(chunks)
        # Stage 2: LLM (if prose chunks exist)
        if any(c.source_type != "code" for c in chunks):
            llm_entities, llm_edges = llm_extract(chunks)
            entities.extend(llm_entities)
            edges.extend(llm_edges)
        # Stage 3: resolve against existing graph
        resolved = resolve_entities(graph, entities)
        graph.upsert(resolved, edges)
        store.mark_graph_extracted(doc.source_path, corpus)
```

### 3.3 LLM Extraction Prompt Strategy

The LLM extraction uses a structured output prompt with the entity type registry:

```
Given the following text chunk from a technical document, extract:
1. Named entities with their type from: {registered_entity_types}
2. Relationships between entities from: {registered_relationship_types}
3. Confidence score (0.0-1.0) for each extraction

Return JSON array. Only extract what is explicitly stated or strongly implied.
Do not infer relationships that require domain expertise beyond the text.
```

The prompt is parameterized by the entity/relationship type registry, so domain extensions automatically expand the extraction vocabulary without modifying core code.

### 3.4 Tree-Sitter AST Extraction

For code files (Python, C++, Fortran — common in simulation codes):

```python
SUPPORTED_LANGUAGES = {"python", "cpp", "c", "fortran", "rust", "go", "mojo"}

def ast_extract(source: str, language: str) -> tuple[list[Entity], list[Edge]]:
    """Deterministic entity extraction from source code AST."""
    tree = tree_sitter_parse(source, language)
    entities = []
    edges = []
    for node in walk(tree):
        if node.type in ("class_definition", "function_definition", "module"):
            entities.append(Entity(label="Code", name=node.name, ...))
        if node.type == "import_statement":
            edges.append(Edge(type="DEPENDS_ON", ...))
    return entities, edges
```

---

## 4. Query Interface

### 4.1 Cypher via AGE

Direct Cypher queries for power users and agent runtimes:

```sql
-- "What procedures govern component V-101?"
SELECT * FROM cypher('axiom_facility', $$
    MATCH (p:Procedure)-[r:GOVERNS]->(c:Component {name: 'V-101'})
    WHERE r.access_tier = 'public' OR r.access_tier = 'restricted'
    RETURN p.name, p.doc_number, r.requirement_type
$$) AS (name agtype, doc_number agtype, req_type agtype);
```

### 4.2 HTTP API

```
POST /api/v1/graph/query
Content-Type: application/json
Authorization: Bearer <federation-token>

{
  "query": "MATCH (p:Procedure)-[:GOVERNS]->(c:Component {name: 'V-101'}) RETURN p",
  "scope": "facility",
  "max_hops": 3,
  "limit": 50
}
```

Response:

```json
{
  "nodes": [...],
  "edges": [...],
  "query_time_ms": 42
}
```

Natural language queries are translated to Cypher by the agent runtime before hitting this endpoint.

### 4.3 CLI

```bash
# Direct Cypher
axi graph query "MATCH (d:Document)-[:REFERENCES]->(r:Document) WHERE r.title CONTAINS 'NUREG' RETURN d.title, r.title LIMIT 10"

# Natural language (agent translates to Cypher)
axi graph ask "what procedures reference valve V-101?"

# Status
axi graph status
# → Nodes: 12,847  Edges: 34,291  Scopes: community(8k), facility(4k), personal(1k)

# Rebuild (re-extract all documents)
axi graph rebuild --corpus rag-org
```

### 4.4 MCP Server Tool

Exposed as an MCP tool so any MCP-compatible coding assistant can query:

```json
{
  "name": "axiom_graph_query",
  "description": "Query the Axiom knowledge graph for entity relationships, dependency chains, and structural navigation across indexed documents.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": "string", "description": "Cypher query or natural language question" },
      "scope": { "type": "string", "enum": ["community", "facility", "personal"] },
      "max_hops": { "type": "integer", "default": 3 },
      "limit": { "type": "integer", "default": 50 }
    },
    "required": ["query"]
  }
}
```

### 4.5 Agent Function Call

Agents (SCAN, TIDY, Neut chat) call the graph via the standard function interface:

```python
# In _rag_context() — hybrid retrieval
async def _rag_context(self, query: str) -> str:
    # Semantic retrieval (existing)
    rag_results = self.store.search(query_embedding, query_text=query)

    # Structural retrieval (new — only if graph is available)
    if self.graph and self._is_structural_query(query):
        cypher = await self._translate_to_cypher(query)
        graph_results = self.graph.query(cypher, scope=self.scope)
        return self._merge_rag_and_graph(rag_results, graph_results)

    return self._format_rag_results(rag_results)
```

---

## 5. Access Tier Enforcement

Every node and edge carries an `access_tier` property inherited from the source content:

```
access_tier = max(source_chunk.access_tier, target_chunk.access_tier)
```

Where `max()` follows the sensitivity ordering: `public < restricted < export_controlled`.

### 5.1 Query-Time Filtering

All graph queries are wrapped in a tier filter before execution:

```python
def _inject_tier_filter(cypher: str, caller_tier: str) -> str:
    """Inject access_tier WHERE clause into Cypher query."""
    allowed = TIER_HIERARCHY[caller_tier]  # e.g., "restricted" → ["public", "restricted"]
    # AGE property filter injected into MATCH clauses
    ...
```

### 5.2 Graph Scope Isolation

Separate AGE graphs per scope (`axiom_community`, `axiom_facility`, `axiom_personal`) provide hard isolation. A community-scope query never touches facility or personal graphs.

Personal graphs are keyed by `owner` property on nodes/edges, enforced at query time.

---

## 6. Federation Graph Merging

### 6.1 Entity Resolution Across Facilities

When merging community-scope graphs from multiple federated peers, entity resolution determines whether "V-101" at Facility A is the same entity as "V-101" at Facility B:

```
Entity Resolution Pipeline:
  1. Exact name match + same entity type → AUTO-MERGE (confidence 1.0)
  2. Fuzzy name match (Levenshtein ≤ 2) + same type + embedding similarity > 0.92 → CANDIDATE
  3. Candidate review:
     a. GREEN (confidence > 0.85): auto-merge, log provenance
     b. YELLOW (0.60-0.85): SCAN resolver — examine context, decide merge/keep-separate
     c. RED (< 0.60): human review queue
```

### 6.2 Graph Sync Protocol

Extends the existing federation sync (see `spec-federation.md`) with graph deltas:

```json
{
  "type": "graph_delta",
  "facility_id": "ut-austin-netl",
  "scope": "community",
  "entities": [
    {"id": "uuid", "label": "Procedure", "name": "NUREG-0800", "properties": {...}}
  ],
  "edges": [
    {"id": "uuid", "type": "GOVERNS", "from": "uuid", "to": "uuid", "properties": {...}}
  ],
  "deleted_ids": ["uuid1", "uuid2"]
}
```

### 6.3 Community Graph Packs

Community-scope graph data is distributed as part of `.axiompack` artifacts:

```
community-knowledge-1.0.0.axiompack
├── manifest.json        (content_type: "rag+graph")
├── chunks.parquet       (RAG chunks — existing)
├── graph_nodes.parquet  (entity table)
├── graph_edges.parquet  (relationship table)
└── SHA256SUMS
```

On install, `install_pack()` loads both chunks into pgvector and graph data into AGE.

---

## 7. Integration with Knowledge Maturity Pipeline

The graph layer connects to the existing maturity pipeline at three points:

### 7.1 Layer 0→1 (Patterns)

TIDY sweep mines `retrieval_log` for co-retrieval patterns. The graph enhances this by providing **structural co-occurrence**: documents that share entities are structurally related regardless of retrieval history.

```
New promotion signal: structural_relatedness
  = count(shared entities between chunk A and chunk B) / max(entities_A, entities_B)
```

### 7.2 Layer 2→3 (Facts → Frameworks)

This is where the graph adds the most value. Layer 3 (Frameworks) requires synthesizing facts into mental models — which is precisely what a knowledge graph represents.

```
Framework generation (CURIO):
  1. Select a cluster of related Layer 2 facts
  2. Query graph for all entities referenced by those facts
  3. Expand 1-hop from those entities to find connecting structure
  4. Synthesize a framework narrative grounded in the subgraph
  5. Store framework as a Layer 3 knowledge fact with graph provenance
```

### 7.3 Contradiction Detection

The graph enables structural contradiction detection that embedding similarity alone cannot:

```
Example:
  Fact A: "Procedure X requires valve V-101 to be OPEN during startup"
  Fact B: "Procedure Y requires valve V-101 to be CLOSED during startup"

  RAG: These chunks may not be cosine-similar (different procedures)
  Graph: Both facts share edge (Fact)-[:VALIDATES]->(Procedure)-[:GOVERNS]->(V-101)
         → structural overlap detected → flag for contradiction review
```

---

## 8. Domain Extension API & Ontology Integration

Axiom core provides the generic entity/relationship type system. Domain-specific types come from one of three sources, in priority order:

### 8.1 Ontology Source Hierarchy

```
Priority 1: External ontology (DeepLynx Nexus, or any OntologyProvider)
             ↓ (if available)
Priority 2: Local ontology cache (~/.axi/ontology/{provider}.json)
             ↓ (if available)
Priority 3: Extension-hardcoded defaults (static fallback)
```

Axiom runs fine with **only Priority 3** — no external dependencies required. Nexus integration is an optional capability that enriches the type system when available.

### 8.2 OntologyProvider Interface

```python
# src/axiom/graph/ontology.py

class OntologyProvider(Protocol):
    """Interface for external ontology sources.

    Axiom's graph layer consumes ontologies — it does not define them.
    The canonical domain ontology lives in the external system (e.g.,
    DeepLynx Nexus). Axiom maps external types to its EntityType /
    RelationshipType model.
    """

    def fetch_entity_types(self, domain: str) -> list[EntityType]:
        """Fetch entity types for a domain from the external ontology."""
        ...

    def fetch_relationship_types(self, domain: str) -> list[RelationshipType]:
        """Fetch relationship types for a domain from the external ontology."""
        ...

    def health_check(self) -> bool:
        """Check if the external ontology is reachable."""
        ...
```

### 8.3 DeepLynx Nexus Provider

1:1 mapping between Nexus and Axiom concepts:

| DeepLynx Nexus | Axiom Graph | Notes |
|---|---|---|
| Container | (scope selector) | Nexus container ≈ Axiom domain/facility scope |
| Metatype | EntityType | 1:1 — each Nexus metatype becomes an Axiom entity label |
| Metatype Key | EntityType.properties | 1:1 — property definitions map directly |
| Metatype Relationship | RelationshipType | 1:1 — each Nexus relationship pair becomes an Axiom edge type |
| Metatype Relationship Key | RelationshipType.properties | 1:1 — edge property definitions |

```python
# src/axiom/graph/providers/deeplynx.py

class DeepLynxOntologyProvider:
    """Fetches ontology from DeepLynx Nexus via REST API or MCP.

    Connection configured via spec-connections.md §3.7 (connections.toml).
    Falls back to cached snapshot if Nexus is unreachable.
    """

    def __init__(self, connection: Connection):
        self._conn = connection  # from connections.toml [deeplynx] or [deeplynx-api]
        self._cache_path = Path("~/.axi/ontology/deeplynx.json").expanduser()

    def fetch_entity_types(self, domain: str) -> list[EntityType]:
        """GET /api/v2/containers/{id}/metatypes → map to EntityType."""
        try:
            metatypes = self._conn.get(f"/containers/{self._container_id}/metatypes")
            entity_types = [self._map_metatype(mt) for mt in metatypes]
            self._update_cache(entity_types)
            return entity_types
        except ConnectionError:
            log.warning("DeepLynx unreachable, using cached ontology")
            return self._load_cache()

    def _map_metatype(self, metatype: dict) -> EntityType:
        """Map a DeepLynx metatype to an Axiom EntityType."""
        # Determine parent from Axiom core types based on metatype category
        parent = self._infer_parent(metatype)  # e.g., "equipment" → Component
        properties = [key["name"] for key in metatype.get("keys", [])]
        return EntityType(
            label=metatype["name"],
            parent=parent,
            properties=properties,
            source="deeplynx",
            nexus_metatype_id=metatype["id"],
        )
```

### 8.4 EntityTypeRegistry

```python
# src/axiom/graph/registry.py

class EntityTypeRegistry:
    """Registry of entity and relationship types for graph extraction.

    Sources (applied in order):
      1. Core types (always present)
      2. OntologyProvider (DeepLynx Nexus or other — if configured and reachable)
      3. Extension-hardcoded defaults (fallback if no provider, or additions beyond ontology)
    """

    def __init__(self):
        self._entity_types: dict[str, EntityType] = {}
        self._relationship_types: dict[str, RelationshipType] = {}
        self._provider: OntologyProvider | None = None
        self._register_core_types()

    def _register_core_types(self):
        """Register Axiom core entity types (always available, no external deps)."""
        for et in [Document, Component, Procedure, Person, Code, Material, Concept, Fact]:
            self._entity_types[et.label] = et

    def load_from_provider(self, provider: OntologyProvider, domain: str) -> int:
        """Load domain-specific types from an external ontology provider.

        Returns the number of types registered. If provider is unreachable,
        falls back to cached ontology. If no cache, returns 0 and logs a
        warning — extraction proceeds with core types only.
        """
        try:
            entity_types = provider.fetch_entity_types(domain)
            rel_types = provider.fetch_relationship_types(domain)
            for et in entity_types:
                self._entity_types[et.label] = et
            for rt in rel_types:
                self._relationship_types[rt.name] = rt
            log.info("Loaded %d entity types, %d relationship types from %s",
                     len(entity_types), len(rel_types), type(provider).__name__)
            return len(entity_types) + len(rel_types)
        except Exception as exc:
            log.warning("Ontology provider unavailable: %s — using core types only", exc)
            return 0

    def register_entity_type(self, entity_type: EntityType) -> None:
        """Register a type directly (extension fallback or additions beyond ontology)."""
        self._entity_types[entity_type.label] = entity_type

    def register_relationship_type(self, rel_type: RelationshipType) -> None:
        """Register a relationship type directly."""
        self._relationship_types[rel_type.name] = rel_type
```

### 8.5 Extension Lifecycle (with Nexus)

```python
# In a domain-consumer extension:
def on_activate(self, ctx: ExtensionContext):
    graph = ctx.get_service("graph")

    # Try DeepLynx Nexus first (if configured in connections.toml)
    nexus = ctx.get_connection("deeplynx")
    if nexus:
        provider = DeepLynxOntologyProvider(nexus)
        loaded = graph.registry.load_from_provider(provider, domain="example")
        if loaded > 0:
            log.info("Ontology loaded from DeepLynx Nexus (%d types)", loaded)
            return  # Nexus is authoritative — no hardcoded overrides

    # Fallback: register types directly (works without Nexus)
    log.info("No ontology provider — registering hardcoded domain types")
    graph.registry.register_entity_type(
        EntityType("Reactor", parent="Component", properties=["type", "thermal_power_mw"])
    )
    graph.registry.register_entity_type(
        EntityType("FuelElement", parent="Component", properties=["enrichment", "geometry"])
    )
    # ... etc
```

### 8.6 Bidirectional Sync (Phase 1.0+)

In the initial phases, the flow is **Nexus → Axiom** (Axiom consumes the ontology). In Phase 1.0+, the flow becomes bidirectional:

- **Nexus → Axiom:** Ontology types, property definitions, relationship vocabulary
- **Axiom → Nexus:** Discovered entities and relationships from operational content (new components found in procedures that aren't yet in the formal ontology)

This creates a feedback loop: operators working with documents surface entities that engineers formalize in the ontology. The protocol for Axiom→Nexus push is deferred to Phase 1.0 when both systems are running in production.

### 8.7 Offline / No Nexus Operation

The graph layer MUST operate fully without Nexus:

| Scenario | Behavior |
|---|---|
| Nexus configured + reachable | Load ontology from Nexus, cache locally |
| Nexus configured + unreachable | Load from `~/.axi/ontology/deeplynx.json` cache |
| Nexus configured + no cache + unreachable | Fall back to extension-hardcoded types + warning |
| Nexus not configured | Extension-hardcoded types only (default) |
| `.axiompack` install (any scenario) | Pack manifest includes ontology snapshot — always works offline |

---

## 9. Performance

### 9.1 Targets

| Operation | Target | Notes |
|---|---|---|
| 2-hop Cypher traversal | < 2s | On 100k-node graph |
| Entity extraction (deterministic) | < 1s per document | Regex + heading parse |
| Entity extraction (LLM) | < 10s per document | Batched, 5 chunks/request |
| Full graph rebuild (10k docs) | < 30 min | Parallelized extraction |
| Graph pack install (10k nodes) | < 60s | Bulk AGE insert |

### 9.2 Indexing

AGE uses GIN indexes on vertex/edge properties. Additional indexes for common access patterns:

```sql
-- Fast entity lookup by name
CREATE INDEX IF NOT EXISTS idx_entity_name ON axiom_community.entity USING gin (properties);
-- Fast tier filtering
-- (AGE handles this internally via property indexes)
```

---

## 10. Migration & Rollout

### 10.1 Phase 0.1 — Schema + Extraction + CLI

- Install Apache AGE on the self-hosted node's PG (alongside pgvector)
- Implement `GraphStore` class (Cypher wrapper, tier filtering)
- Implement deterministic extraction pipeline (cross-references, headings, metadata)
- Implement `axi graph query`, `axi graph status`, `axi graph rebuild`
- Add `graph_extracted_at` column to `documents` table for incremental tracking
- Tests: unit tests for extraction, integration tests against PG+AGE

### 10.2 Phase 0.2 — Hybrid Retrieval + MCP

- Implement LLM-assisted extraction with confidence scoring
- Wire graph query into `_rag_context()` hybrid retrieval
- Implement query router (structural vs. semantic classification)
- Expose `axiom_graph_query` MCP tool
- Add graph data to `.axiompack` format (graph_nodes.parquet, graph_edges.parquet)

### 10.3 Phase 1.0 — Federation + Community Graph

- Implement entity resolution pipeline
- Add graph delta to federation sync protocol
- Implement community graph pack generation and install
- Wire into SCAN for Layer 2→3 framework synthesis
- Wire into contradiction detection

### 10.4 Backward Compatibility

The graph layer is entirely additive:
- AGE extension is installed alongside pgvector; no schema changes to existing tables
- `documents` table gets one new nullable column (`graph_extracted_at`)
- All existing RAG functionality is unaffected if graph is not installed
- Feature-gated: `if graph_available()` checks before any graph code path

---

## 11. Open Questions

1. **AGE stability on PG 16.** Need to validate AGE 1.5+ on the self-hosted PG 16 instance before committing. Fallback: recursive CTEs with a `graph_nodes` + `graph_edges` relational schema (loses Cypher but keeps functionality).
2. **Entity embedding strategy.** Should graph entities get their own embeddings (entity name + context → 768-dim vector), or should entity resolution rely solely on fuzzy string matching? Dedicated embeddings improve resolution quality but add storage and compute cost.
3. ~~**DeepLynx handoff.**~~ **Resolved.** Nexus is the preferred (but optional) ontology source via `OntologyProvider` interface. Nexus metatypes map 1:1 to Axiom EntityTypes. Axiom→Nexus push deferred to Phase 1.0. See §8.
4. **Graph visualization.** The PRD does not require a graph UI, but export to interactive HTML (vis.js) or Obsidian vault would be high-value. Defer to post-1.0 unless demand surfaces earlier.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
