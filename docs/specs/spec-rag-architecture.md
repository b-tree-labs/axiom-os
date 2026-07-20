# Axiom RAG Architecture Spec

**Status:** Active — Phase 1 schema migration pending
**Owner:** Ben Booth
**Created:** 2026-03-12
**Last Updated:** 2026-05-27 (added: ingest engine + provenance gate + corpus audit, v0.22.0)
**Related:** `spec-model-routing.md`, `prd-agents.md`, `spec-rag-knowledge-maturity.md`, `spec-prompt-registry.md`, `adr-014-rag-tiered-local-cache.md`, `spec-rag-ingest-advanced.md`

---

> **Ingest + governance update (v0.22.0, 2026-05-27).** The ingestion side has
> advanced well past this spec's original "Phase 1" framing:
> - **Durable ingest engine** — `axi rag ingest` (preflight, checkpoint/resume,
>   calibration/ETA, progress stream); see `spec-rag-ingest-advanced.md`.
> - **Provenance/artifact gate** — `axiom.rag.ingest_router` decides
>   exclude/quarantine/allow(+tier) per file by *source and artifact type*
>   (a `--rules` set), wired into the ingest path so controlled/proprietary
>   content is gated before it is read. **EC determination is provenance-based;
>   the keyword screener (`ec_screening`, §8) is now a *secondary* signal.**
> - **Safe-by-default** — shared-tier (`rag-org`/`rag-community`) ingest refuses
>   without provenance rules.
> - **`axi rag audit`** — find/purge controlled content already in a corpus.
> - **Durability** — embed-failure no longer commits a doc text-only (it retries);
>   honest per-reason drop reporting.
>
> The §12 phase checkboxes below predate this and are being reconciled; the
> CHANGELOG (v0.22.0) and `spec-rag-ingest-advanced.md` are authoritative for
> ingest status.

---

## 1. Problem Statement

Axiom has a working RAG store (`src/axiom/rag/`) using pgvector. It has
two unresolved architectural gaps that become critical as the platform grows:

**Gap 1 — No scope model.**
The store's `tier` column conflates two independent concerns: content sensitivity
(`public | export_controlled`) and content scope (`community | facility | personal`).
Without separating these, it's impossible to build the per-user personalization that
makes Neut irreplaceable in daily use. This affects all deployments.

**Gap 2 — Cloud embedding of sensitive content (EC deployments only).**
`embeddings.py` always calls the OpenAI cloud API. For deployments handling
export-controlled documents (SimTool inputs, facility-specific procedures, licensed
simulation materials), this constitutes an unauthorized export of the content
itself — not just the query. Non-EC deployments are unaffected.

**Gap 2b — EC files cannot be copied to a user's local machine (EC deployments only).**
Under EAR and 10 CFR 810, the act of copying EC material off an authorized computing
environment is itself a transfer — even if the destination is a personal machine with
no external network access. EC documents must remain on authorized systems. This
invalidates any architecture that proposes "local embedding" of EC files on a user's
workstation. EC ingest, embedding, and storage must all run on the authorized system.

> **Current state:** The private server Private-Server (a self-hosted node at the deploying org running
> Qwen via Ollama) serves as the no-cloud environment for the **restricted** tier. A
> proper export-controlled / classified environment (PrivateCloud or equivalent) is a future
> design item and has not yet been built. The architecture is designed to accommodate
> it when ready. References to "authorized EC environment" throughout this spec describe
> the target design; Private-Server represents the current restricted-tier implementation.

> **Non-EC deployments:** Single-store mode (local postgres) is the default. The
> restricted/EC store, private server connection, and dual-store retrieval are opt-in
> features activated only when a restricted-tier provider is configured. No
> configuration or infrastructure beyond local postgres and an embedding provider is
> required for standard operation.

---

## 2. Two-Dimensional Content Model

Every document in the RAG store has two independent attributes:

### 2.1 Access Tier (sensitivity axis)

| `access_tier` | Meaning | Embedding pipeline | Store location | Retrieval gate |
|---------------|---------|-------------------|----------------|----------------|
| `public` | Safe for cloud processing | Cloud (OpenAI, Anthropic) or local Ollama | Local postgres or cloud | Any authenticated user |
| `restricted` | No-cloud; private server only | Ollama on **private server** (e.g. Private-Server running Qwen) — never cloud | Private server postgres (e.g. Private-Server) | `restricted_access` role + VPN |
| `export_controlled` | EAR/10 CFR 810 regulated; classified/export-controlled material | Ollama on **authorized EC environment** (PrivateCloud — future, not yet built) — never cloud | Authorized EC environment postgres (PrivateCloud — future) | `export_controlled_access` role + VPN + authorized system |

> **Compliance note:** EC material cannot be copied to a user's workstation under any
> circumstances. All EC ingest, embedding, and storage must execute on an authorized
> system. Retrieval is proxied via VPN; only the LLM-synthesized response crosses the
> boundary.
>
> **Current implementation:** Private-Server (a self-hosted org node, running Qwen via Ollama)
> is the implementation of the **restricted** tier — it provides no-cloud LLM for
> sensitive-but-not-classified content. The `export_controlled` tier (PrivateCloud or equivalent
> authorized computing environment) is a future design item. The architecture separates
> these tiers by design so that PrivateCloud can be wired in when available without restructuring.

### 2.2 Scope (visibility axis)

| `scope` | Who can retrieve | Examples |
|---------|----------------|---------|
| `community` | Everyone (within their tier) | regulatory body regs, published papers, IAEA guides, system physics reference, axiom docs |
| `facility` | Members of this facility | Facility procedures, local configs, facility meeting history |
| `personal` | Only the document owner | User notes, personal papers, individual session context |

### 2.3 The 2×3 Matrix

|  | `community` | `facility` | `personal` |
|--|------------|-----------|-----------|
| **`public`** | Shipped with Neut, curated domain-specific knowledge base | Non-sensitive facility docs | User's public notes and papers |
| **`export_controlled`** | Licensed simulation docs (SimTool manuals, etc.) | EC facility procedures, sim configs | User's EC work files, run outputs |

---

## 2a. Three-Tier Corpus Architecture

The `scope` axis from §2.2 maps to three named corpora, each with a stable `corpus_id` used throughout the codebase and CLI:

| Corpus | `corpus_id` | `scope` | Content | Built when |
|--------|-------------|---------|---------|------------|
| Community | `rag-community` | `community` | Pre-indexed domain-specific domain knowledge (~33k chunks); ships bundled with the pip package | `axiom setup` step 5a (`axiom rag load-community`) |
| Organization | `rag-org` | `facility` | Facility-specific docs; synced from private-server/S3 by admin | `axiom rag sync org` |
| Personal | `rag-internal` | `personal` | User's workspace: `docs/`, `runtime/knowledge/`, Python docstrings (via AST) | `axiom rag index .` during install + post-push |

All three corpora are queried together on every retrieval call. When the same content exists in multiple corpora, personal results take priority over org, org over community.

### Priority and Conflict Resolution

```
rag-internal  (personal)    → highest priority — user's own workspace
rag-org       (facility)    → overrides community on facility-specific topics
rag-community (community)   → baseline domain-specific domain knowledge
```

Priority is implemented at result-merging time: when two chunks have near-identical content (cosine similarity > 0.97) and different `corpus_id` values, the higher-priority corpus's chunk is returned and the lower-priority duplicate is suppressed.

---

## 3. Architecture

Two physically separate stores are required by export control compliance — not just
a logical separation. EC material cannot exist on a user's workstation.

```mermaid
flowchart TB
    subgraph Client["Client (user workstation)"]
        Q[User query]
        QC[Query classifier — router.py]
        Q --> QC
    end

    subgraph PublicPath["Public path"]
        QC -->|public| QE1[Embed query — cloud or local Ollama]
        QE1 --> S1["Public RAG store — local postgres — access_tier=public"]
        S1 --> R1[Chunks → cloud LLM — Anthropic]
    end

    subgraph ECPath["Restricted/EC path VPN required"]
        QC -->|restricted or — export_controlled| VPN[VPN tunnel — org network]
        VPN --> RAPI["Retrieval API — on private server (Private-Server today; PrivateCloud for classified)"]
        RAPI --> S2["Restricted RAG store — private server postgres — access_tier=restricted (or export_controlled when PrivateCloud is built)"]
        S2 --> LLM2["Qwen on private server (Private-Server today; PrivateCloud LLM for classified)"]
        LLM2 --> Resp[Response only — crosses VPN boundary]
    end

    subgraph PrivateIngest["Restricted Ingest — private server only"]
        ED[Restricted/EC Document — on private server]
        OL[Ollama on private server — nomic-embed-text]
        ED --> OL --> S2
    end
```

**Key invariants:**

1. **Restricted/EC text never touches a cloud API** and **never leaves the private/authorized
   environment.** Under EAR/10 CFR 810, copying EC material to a client workstation is itself
   an unauthorized transfer. All restricted/EC ingest, embedding, storage, and retrieval happen
   on the private server (Private-Server for the restricted tier today; PrivateCloud for the classified/EC tier
   when built).
2. **Two physical stores, same logical schema.** Both run pgvector with identical
   `access_tier`/`scope` columns. The public store is local; the restricted/EC store is on the
   private server. The client connects to each via different connection strings.
3. **Query embedding must match document embedding.** Public queries embed on cloud/local
   and search the local store. Restricted/EC queries embed on the private server and search the
   private server store.
4. **Only the synthesized response crosses the VPN boundary.** The private-server LLM (Qwen on
   Private-Server) runs server-side; its text output returns to the client. Retrieved restricted/EC
   chunks are consumed server-side. Whether chunk text appears in the response is a facility
   policy decision (redaction scope).
5. **Personal restricted/EC content follows the same rule.** A user's restricted or EC work
   files must be indexed on the private server, not on their local machine, even if they
   authored the files.

---

## 3a. Local Store (DuckDB)

The local store is a DuckDB database with the `vss` extension for vector similarity
search. It lives at `~/.axi/rag/local.duckdb` (or `$AXI_HOME/rag/local.duckdb`).
It holds:

- **`rag-internal` corpus:** always synced here first; this IS the primary store for
  personal content.
- **`rag-org` pack cache:** chunks loaded from downloaded `.axiompack` files; never
  written by ingestion directly.

### Schema (DuckDB)

```sql
CREATE TABLE chunks (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_path     TEXT        NOT NULL,
    chunk_index  INT         NOT NULL,
    text         TEXT        NOT NULL,
    corpus       TEXT        NOT NULL,  -- rag-internal | rag-org
    pack_id      TEXT,                  -- NULL for rag-internal; pack version for rag-org
    checksum     TEXT,
    indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding    FLOAT[768]             -- NULL if no embedding model available
);

CREATE INDEX ON chunks (corpus, doc_path);
-- vss index created post-load when embeddings present:
-- CALL vss.create_index('chunks', 'embedding');
```

Pack manifest table:

```sql
CREATE TABLE installed_packs (
    pack_id      TEXT PRIMARY KEY,
    domain_tag   TEXT NOT NULL,
    version      TEXT NOT NULL,
    installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fact_count   INT,
    sha256       TEXT NOT NULL
);
```

The DuckDB local store is the default for all personal content and pack cache. It
requires no PostgreSQL instance and works fully offline. The server-side PostgreSQL
store (§4) is additive — used for community corpus, restricted/EC content, and
(eventually) IAM-gated server-side sync.

---

## 3b. Query Fan-out

At query time the RAG retriever fans out across available stores:

```
User query
    │
    ├── Local DuckDB (always) ──────────────────→ local_results
    │   [rag-internal + installed rag-org packs]
    │
    ├── Remote PostgreSQL (if reachable) ────────→ remote_results
    │   [full rag-org + rag-community]
    │
    └── PrivateCloud endpoint (if routing_tier=export_controlled) → ec_results
        [rag-export-controlled, identity-gated]

    Merge by relevance score → top-k chunks → inject into prompt
```

If remote is unreachable: local results only. Graceful degradation, never a hard
failure.

The merge step applies corpus priority as defined in §2a: `rag-internal` results
score highest when content is near-duplicate across corpora, then `rag-org`, then
`rag-community`. Score merging is done at result time — no cross-store joins required.

---

## 3b-2. Dynamic RAG Routing — Policy-Driven Corpus Selection (built 2026-04-16)

> **Implementation status: ✅ Built** — `src/axiom/extensions/builtins/classroom/rag_policy.py`,
> 11 tests in `tests/classroom_unit/test_rag_policy.py`. Zero stubs.

The static fan-out in §3b is the DEFAULT behavior. For classrooms,
research experiments, A/B tests, and fine-grained corpus control,
`PolicyAwareRetriever` replaces the static fan-out with a
**policy-driven, runtime-swappable** retrieval layer.

**Core data model:**

- `RAGPolicy` — named, versioned policy declaring: which corpora to
  query (ordered, weighted), what void rules to apply, whether a
  shadow/parallel path runs for A/B comparison.
- `PolicyAwareRetriever` — reads the active policy on every
  `retrieve()` call. Runtime swap via `set_policy()` — no restart.

**Five capabilities:**

| Capability | What it does | Status |
|---|---|---|
| Policy-driven selection | Named policy declares corpora + order + weights | ✅ Built |
| Multi-corpus merging | Query N corpora, merge results; missing corpora skip gracefully | ✅ Built |
| Void rules | Time-bounded subset exclusion by tag/source; auto-reinstate on expiry; "void ch5-7 during quiz" | ✅ Built |
| Shadow/parallel path | Same prompt also hits a shadow corpus (e.g. a full consumer-extension RAG) async; results captured to LangFuse for A/B comparison; student never sees shadow | ✅ Built |
| Runtime swap | `set_policy()` takes immediate effect on next retrieve() — no restart | ✅ Built |

**Void rule filter patterns:** `tag:X` (tag match), `source:X`
(source match), `*` (void entire corpus). Rules carry `starts_at`
and `expires_at`; expired rules auto-reinstate. Voiding never
deletes data.

**Shadow path resilience:** shadow failure is swallowed — never
blocks the primary retrieval path. Shadow results captured via
callback (→ LangFuse trace, tagged `shadow`).

**Classroom use case (Prague NE):** primary = course-materials-only
RAG (from-zero, curated by instructor); shadow = full consumer-extension RAG
(public + org). Every student prompt hits both paths; only primary
serves the student. LangFuse shows primary-vs-shadow per-query for
meta-learning. Instructor can swap policy mid-course without
restart.

**Instructor RAG onboarding:** during `axi classroom create`, the
instructor is guided through RAG configuration (course-only vs
course+institutional vs full; shadow yes/no; assessment void
schedule). This is an LLM-modulated interactive flow.

---

## 3c. Pack Format (.axiompack)

A `.axiompack` file is a gzip-compressed tar containing:

- `manifest.json` — pack metadata (matches community spec §7.4)
- `chunks.parquet` — pre-chunked, pre-embedded content
- `SHA256SUMS` — integrity verification

Install: `axiom rag pack install <file.axiompack>` loads chunks into local DuckDB and
records in `installed_packs`.

```bash
axiom rag pack install ./domain-specific-regs-v2.axiompack   # load pack into local DuckDB
axiom rag pack list                                  # list installed packs with chunk counts
axiom rag pack remove <pack_id>                      # unload pack and delete from local store
```

The pack format enables offline distribution of org corpus snapshots without requiring
a running server or admin credentials. A facility admin exports a pack; users install it
locally. Packs are read-only at query time — user ingestion never writes to the pack
cache rows; it only writes to `rag-internal`.

---

## 3d. IAM Dependency

The following capabilities require IAM and are deferred:

| Capability | IAM requirement |
|---|---|
| Automatic `rag-internal` sync to server-side user account | Per-user identity + namespace |
| Pack entitlement checks | Authorization service: which packs a user may download |
| Per-user namespacing in server-side PostgreSQL | User account provisioning |
| PrivateCloud access gating | Network + identity (both required) |

Until IAM ships:

- `rag-internal` is **local-only** (no automatic sync)
- Manual portability: `axiom rag export --personal` / `axiom rag import --personal <file>`
- Packs are downloaded without entitlement enforcement (honor system)
- PrivateCloud access is controlled by network reachability + API key only

This means Phase 0 (see §12) can ship and deliver value independently of IAM
infrastructure. No IAM plumbing is needed to use local DuckDB, install packs, or
run the query fan-out against a reachable remote.

---

## 4. Schema Evolution

### Current schema (existing)

```sql
-- chunks table has:
tier    TEXT NOT NULL DEFAULT 'institutional'   -- ambiguous; being repurposed
owner   TEXT                                    -- personal scope marker
```

### Target schema

```sql
ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS access_tier TEXT NOT NULL DEFAULT 'public',
    ADD COLUMN IF NOT EXISTS scope       TEXT NOT NULL DEFAULT 'community';

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS access_tier TEXT NOT NULL DEFAULT 'public',
    ADD COLUMN IF NOT EXISTS scope       TEXT NOT NULL DEFAULT 'community';

-- Migrate existing 'institutional' tier → 'public' access_tier, 'community' scope
UPDATE chunks SET access_tier = 'public', scope = 'community' WHERE tier = 'institutional';
UPDATE documents SET access_tier = 'public', scope = 'community' WHERE tier = 'institutional';

CREATE INDEX IF NOT EXISTS idx_chunks_access_tier ON chunks (access_tier);
CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks (scope);
```

The legacy `tier` column is kept for a transition period, then dropped.

---

## 5. Embedding Provider Abstraction

`src/axiom/rag/embeddings.py` evolves from a single cloud function to a
provider-aware interface:

```python
def embed_texts(
    texts: list[str],
    access_tier: str = "public",          # routes to cloud or local
    model: str | None = None,             # override; None = use default for tier
) -> list[list[float]] | None:
    """Embed texts using the appropriate provider for the access tier.

    public           → cloud API (OpenAI text-embedding-3-small)
    export_controlled → local Ollama (nomic-embed-text or configured model)
    """
```

**Restricted-tier embedding provider (Ollama on private server, e.g. Private-Server):**
- Runs on the private server — never on the user's workstation or a cloud API
- Model: `nomic-embed-text` (768 dims) or `mxbai-embed-large` (1024 dims)
- The same Ollama instance used by `OllamaClassifier` for restricted/EC query classification
- Ingest pipeline for restricted content runs as a server-side job on the private server (not a client CLI command)

> **Note:** Private-Server is the current implementation of the restricted tier (a self-hosted org
> node, running Qwen). The classified/export-controlled tier would use PrivateCloud as the
> authorized computing environment — PrivateCloud integration is a future design item. When PrivateCloud
> is available, a separate embedding provider entry pointing to the PrivateCloud Ollama instance
> handles the `export_controlled` tier.

**Compliance boundary (current: Private-Server for restricted tier):**
```
client workstation          private-network VPN / private server (Private-Server)
──────────────────          ────────────────────────────────────────────────
axiom chat                   restricted document files
query → router              Ollama (nomic-embed-text)
                            pgvector restricted store
                  ←VPN←    Qwen on Private-Server
synthesized response        (LLM runs here; response crosses VPN boundary)

                            [Future: PrivateCloud for export_controlled tier]
```

**Dimension note:** EC store uses 768-dim vectors (Ollama); public store uses 1536-dim (OpenAI)
or 768-dim (local Ollama for public content). Stores are physically separate, so mixed
dimensions across stores are not a problem. Each store's index is internally consistent.

---

## 6. Community Corpus (`rag-community`)

The community corpus is the differentiator that makes onboarding immediately valuable.
It ships pre-indexed as a bundled artifact and is loaded during `axiom setup`.

### 6.1 Content (v1 — ~33,000 chunks)

| Category | Documents | `access_tier` |
|----------|-----------|--------------|
| regulatory body regulations | 10 CFR Parts 50, 830 | `public` |
| DOE standards | DOE-STD-1066, DOE-STD-3009 | `public` |
| IAEA safety reports | Selected series | `public` |
| Simulation codes | SimTool6 user manual, AnalysisTool overview, ORIGEN chain docs | `public` |
| V&V | ASME V&V 10 | `public` |
| Neut documentation | All of `docs/` in this repo | `public` |

No export-controlled content is included in the bundled community corpus. Licensed code manuals (SimTool with LANL license, etc.) are facility-provided and indexed separately into `rag-org`.

### 6.2 Distribution

The community corpus ships as a compressed PostgreSQL dump bundled inside the pip package:

```
src/axiom/data/rag/community-v1.pgdump.gz
```

The dump contains the `rag_community` schema with pre-embedded vectors. Loading it does not require re-embedding — the dump restores directly into the local PostgreSQL instance.

```bash
axiom rag load-community          # decompress + pg_restore into rag_community schema (~30s)
```

This command is called automatically during `axiom setup` step 5a. It is safe to re-run; on re-load the old schema is renamed to `rag_community_prev` before the new load (enabling one-version rollback).

### 6.3 Versioning and Upgrade

The community corpus is versioned with Neut releases. `axiom update` checks whether the installed corpus version matches the package version and loads a newer dump if available.

Upgrade path:

```sql
-- Before loading new community dump:
ALTER SCHEMA rag_community RENAME TO rag_community_prev;
-- pg_restore into new rag_community schema
-- After validation:
DROP SCHEMA rag_community_prev CASCADE;   -- manual cleanup after confirming upgrade
```

One previous version is retained to allow rollback. Two-version-old schemas are dropped automatically on the next upgrade.

### 6.4 Deployment Roadmap

The bundled pgdump approach (v1) supersedes the earlier private-server rsync strategy. Future phases add delta sync and CDN delivery but maintain the same `axiom rag load-community` interface.

| Phase | Mechanism | Notes |
|-------|-----------|-------|
| **v1 — bundled pgdump** | Ships in pip package; `axiom rag load-community` | ~30s restore, no network needed |
| **v2 — private-server snapshot** | Manual `rsync` + `axiom rag index` for non-bundled content | VPN required |
| **v3 — S3** | `axiom rag sync community` auto-downloads versioned artifact | AWS approval pending |
| **v4 — CDN** | Hosted artifact, delta sync, checksummed | Long-term |

### 6.5 Out-of-Box Experience

First `axiom chat` after install:
```
Community knowledge base loaded (33,000 chunks)
   Topics: regulatory body regs, DOE standards, IAEA reports, SimTool6, AnalysisTool, Neut docs
   Org knowledge: not configured (run: axiom rag sync org)
   Personal knowledge: not indexed (run: axiom rag index . to add your workspace)
```

---

## 6b. Organization Corpus (`rag-org`)

The org corpus contains facility-specific documents maintained by an admin. It maps to `scope = 'facility'` and is visible to all authenticated members of the facility.

**Content:** Facility procedures, local configurations, licensed code manuals, facility meeting history, site-specific regulatory correspondence.

**Sync sources (roadmap):**

| Phase | Mechanism |
|-------|-----------|
| v1 | Manual: `axiom rag sync org --source private-server` — rsync from private-server mountpoint |
| v3 | `axiom rag sync org --source s3` — S3 bucket configured by admin |
| v4 | CDN-hosted org snapshot with delta sync |

```bash
axiom rag sync org                # pull latest org corpus snapshot (source from config)
axiom rag sync org --source private-server:/axiom/org-rag/  # explicit source override
```

Admin configures the org sync source in `runtime/config/secrets.toml`:
```toml
[rag.org]
sync_source = "private-server:/axiom/org-rag/"   # or s3://bucket/path/
```

The org corpus is indexed into `rag_org` schema in the local PostgreSQL instance for public-access_tier content, and into the private server PostgreSQL instance (Private-Server today) for restricted-tier facility documents.

---

## 7. Personal RAG (Onboarding Augmentation)

During `axiom config` (setup wizard), the user is offered:

```
Would you like Neut to index your documents for personalized retrieval?
Neut will index public documents you specify — your notes, papers, non-sensitive files.

  Public documents (notes, papers, non-sensitive files):
    Indexed locally on this machine. Never sent to cloud APIs.

  Restricted documents (SimTool inputs, sim configs, licensed materials):
    Must remain on the private server (Private-Server/VPN environment).
    Indexing runs server-side via `axiom rag index --remote`.
    You access them only while connected to private-network VPN.

  [Y] Yes, index my public documents now
  [e] Set up restricted-tier indexing on private server (requires VPN)
  [n] Skip for now
```

### 7.1 Compliance Boundary for Personal RAG

EC files created or used by an individual must still stay in the authorized environment.
If a user has SimTool input files on their workstation, they are in violation of handling
requirements — that's a facility policy issue, not a Neut design issue. Neut will not
offer to index files on the local machine as export-controlled. Instead:

```
User: "I have SimTool inputs I want to index"
Neut: "Restricted files must be indexed from the private server.
       Copy them to the private server (e.g. Private-Server) first,
       then run: axiom rag index --remote <path>"
```

### 7.2 Ingest Commands

```bash
# Public personal documents (indexed locally)
axiom rag index ./my-notes/                          # auto-classify; public only
axiom rag index ./facility-procedures/ --scope facility  # facility-wide visibility

# Restricted documents (indexed on private server via VPN — e.g. Private-Server)
axiom rag index --remote private-server:/home/user/mcnp-inputs/  # runs server-side on private server
axiom rag index --remote private-server:/home/user/sim-configs/ --tier restricted

axiom rag status                                     # show index stats (both stores)
axiom rag list                                       # list indexed documents
axiom rag remove ./old-notes/                        # remove from public index
```

### 7.2 Auto-Classification During Ingest

When `--tier` is not specified, the export control router classifies each document
before embedding:

```python
# ingest.py
tier = router.classify(content[:2000]).tier.value  # sample first 2000 chars
embeddings = embed_texts(chunks, access_tier=tier)
```

User is shown what was classified and may override before committing.

### 7.3 Personal Corpus Sources (Implemented)

The personal corpus (`rag-internal`) is built automatically from four source types.
All ingest is **fully asynchronous** — no blocking in the prompt/response chain.

| Source | Path | Trigger | `source_type` |
|--------|------|---------|--------------|
| Chat session transcripts | `runtime/sessions/*.json` | After every chat turn (daemon thread) | `session` |
| Processed signal outputs | `runtime/inbox/processed/*.json` | `axiom rag index` / watch | `signal` |
| Git commit logs | `.git` repos under `runtime/knowledge/` | `axiom rag index` / watch | `git-log` |
| Daily notes | `runtime/knowledge/notes/YYYY-MM-DD.md` | `axiom note "..."` (immediate, background) | `markdown` |

**Session indexing** — `ChatAgent._schedule_session_index()` spawns a `daemon=True`
thread after each completed turn. Checksum deduplication means unchanged turns cost
nothing on re-runs. Sessions with fewer than 3 turns are skipped as noise.

**Watch mode** — `axiom rag watch` runs a `watchdog` filesystem observer across
`docs/`, `runtime/knowledge/`, `runtime/sessions/`, and `runtime/inbox/processed/`.
Events are debounced (2 s window) to handle editor temp-file swaps.

**Notes** — `axiom note "thought"` appends a timestamped entry to the daily markdown
file and triggers background re-indexing. `axiom note` (bare) opens `$EDITOR`.

**Low-confidence hint** — when the best `combined_score < 0.15`, the system prompt
appends: `[Low RAG confidence — run axiom rag index or axiom note to add more context]`

**Implementation** — `src/axiom/rag/personal.py` contains `ingest_sessions()`,
`ingest_signals()`, `ingest_git_logs()`. `ingest_repo()` calls all three when
`personal=True` (default for `rag-internal`; set `False` for community/org corpora).

### 7.4 Corpus Lifecycle — TIDY Stewardship

The personal corpus grows without bound unless actively managed. TIDY owns corpus
health as a scheduled stewardship task, analogous to how it manages `archive/` and
`spikes/`.

**TIDY responsibilities:**

| Task | Schedule | Command |
|------|----------|---------|
| Nightly incremental index | Daily, off-hours | `axiom rag index` (checksum-skipping, fast) |
| Session pruning | Weekly | Delete `sessions/` corpus entries older than N days (configurable `rag.session_ttl_days`) |
| Corpus health check | On `axiom status` | Detect source/index drift; report stale document count |
| Watch daemon supervision | On login | Start `axiom rag watch`; restart on crash (launchd/systemd) |
| Index size reporting | On `axiom status` | Surface chunk counts without requiring `axiom rag status` |

**Watch daemon installation** — during `axiom config`, TIDY generates and installs:
- **macOS**: `~/Library/LaunchAgents/io.axiom.rag-watch.plist` (launchd)
- **Linux**: `~/.config/systemd/user/axiom-rag-watch.service` (systemd user unit)

Both supervise `axiom rag watch --quiet` and restart on exit.

**Session TTL** — configurable via:
```bash
axiom settings set rag.session_ttl_days 90   # default: 90
```
TIDY's weekly sweep calls `store.delete_corpus_older_than(CORPUS_INTERNAL, days=ttl)`
(to be implemented in `rag/store.py`).

*Cross-reference: `spec-agent-architecture.md` §TIDY Corpus Stewardship*

### 7.5 What Makes Neut Irreplaceable

The personal RAG compounds over time. After 6 months of use:
- Every meeting the user attended is indexed and retrievable
- Every document they ingested is searchable
- Every chat session is retrospectively searchable
- Community content is always fresh (auto-updated)
- Facility context (procedures, configs) is mixed in automatically

A query like "what's the last time we discussed xenon poisoning in a meeting?" or
"find me the relevant regulatory body reg for this operating limit" works across all three scopes
simultaneously, filtered by what the user is authorized to see.

---

## 7b. `axiom rag` CLI Reference

All RAG operations go through the `axiom rag` noun. Commands that write to the database require a configured `rag.database_url`.

```bash
# ─── Corpus loading ───────────────────────────────────────────────
axiom rag load-community          # decompress + pg_restore bundled community dump (~30s)
                                 # called automatically by axiom setup step 5a

axiom rag sync org                # pull org corpus snapshot (source from config)
axiom rag sync org --source <url> # explicit source: private-server:/path or s3://bucket/path

# ─── Personal corpus ──────────────────────────────────────────────
axiom rag index [path]            # index path into rag-internal; includes sessions,
                                 # signals, git logs, and notes automatically
axiom rag index --remote <path>   # server-side index for EC content on private-server
axiom rag watch                   # foreground watcher: re-indexes changed files live
                                 # (TIDY installs this as a launchd/systemd daemon)

# ─── Notes (personal knowledge capture) ────────────────────────────
axiom note "quick thought"        # append timestamped note to today's daily file,
                                 # index in background (zero prompt-chain impact)
axiom note                        # open $EDITOR for longer note, then index
axiom note --list                 # show recent daily note files

# ─── Search and inspection ────────────────────────────────────────
axiom rag search <query>          # hybrid search across all three corpora
axiom rag status                  # chunk counts per corpus (rag-community / rag-org / rag-internal)
axiom rag list                    # list indexed documents with corpus, scope, tier
axiom rag remove <path>           # remove path from rag-internal index

# ─── Maintenance (TIDY scheduled) ──────────────────────────────────
axiom rag reindex                 # clear corpus and rebuild from all sources
axiom rag reindex --corpus rag-internal --model <model>  # re-embed with new model
```

`axiom rag status` output example:

```
corpus          scope       chunks   last_updated
rag-community   community   33,241   2026-03-10 (v1.2.0)
rag-org         facility     4,108   2026-03-11
rag-internal    personal     1,834   2026-03-13
```

---

## 8. Export Control Compliance Requirements

> This section captures compliance constraints that MUST be enforced by design.
> These are not aspirational — they are hard architectural requirements.

### 8.1 What the Regulations Require

| Regulation | Requirement | Neut implication |
|------------|-------------|-----------------|
| EAR (15 CFR 730-774) | Controlled technology may not be released to unauthorized persons or locations | EC documents cannot be copied to a user's workstation; cannot transit cloud APIs |
| 10 CFR 810 | Unclassified domain-specific technology requires DOE authorization for transfer | Same as EAR for domain-specific-specific codes (SimTool, ORIGEN, etc.) |
| Facility license | Facility-specific SLAs with LANL/ORNL/etc. for licensed code manuals | Manuals must stay in controlled environment per license terms |

### 8.2 Prohibited Operations (by design)

The following operations MUST be prevented by the architecture — not just policy:

| Operation | Why prohibited | Design control |
|-----------|----------------|----------------|
| `axiom rag index ./mcnp-inputs/` (local) | Copies restricted/EC file content to local postgres | Router classifies → `--remote` flag required for restricted/EC |
| Sending restricted/EC chunk text to cloud LLM | Transmits restricted/EC content to unauthorized service | Restricted/EC queries only reach private server LLM on VPN |
| Downloading restricted/EC chunks to display in terminal | Retrieved restricted/EC text on client workstation | Facility policy decision; Neut default: display only LLM-synthesized response |
| Restricted/EC embedding via OpenAI API | Transmits restricted/EC content to cloud | Restricted/EC embedding runs on private server Ollama only |

### 8.3 Authorized Restricted/EC Data Flow

```
1. Restricted/EC document exists on private server (Private-Server for restricted;
   PrivateCloud for export_controlled — future)
2. Ingest job runs on private server:
   classify → embed (Ollama on private server) → store (private server postgres)
3. User connects via private-network VPN
4. User query classified as restricted/EC by router.py
5. Query embedding computed on private server (via retrieval API)
6. Similarity search on private server postgres
7. Top chunks fed to LLM on private server (Qwen on Private-Server today)
8. LLM generates response — only the response crosses VPN boundary
9. Response displayed to user

Note: Steps 1–8 all execute on the private server. The only data that
crosses the VPN is the synthesized text response in step 9.
Classified (export_controlled) content would follow the same flow on PrivateCloud
once that environment is built.
```

### 8.4 Open Policy Questions (facility must decide)

These require facility radiation protection / export control officer input:

1. **Are retrieved EC chunk texts considered a controlled transfer?**
   If yes: only synthesized LLM responses may cross the VPN (default Neut behavior).
   If no: raw chunk text may be returned to the client for display (more useful, riskier).

2. **Does the classification of the synthesized response need to be marked?**
   If the LLM synthesizes a response that substantially reproduces EC content, does
   that response inherit an EC classification? Current approach: mark responses from
   the EC retrieval path with `[Export-Controlled Environment]` prefix.

3. **Can researchers index their restricted work files from the private server home directories?**
   Yes — they're already on the private server (Private-Server). `axiom rag index --remote` triggers
   a server-side job. Authentication is via existing SSH key / private server credentials.
   For genuinely classified/EC content, the same principle applies on PrivateCloud when that
   environment is available.

### 8.5 Prompt Injection Defense

RAG-augmented systems are vulnerable to prompt injection via malicious content in indexed
documents. For restricted/EC RAG, this is also an exfiltration vector — a poisoned document
on the private server could instruct the LLM to reproduce controlled content in its response.

Defense layers:
1. **Chunk sanitization** — strip known injection patterns before LLM injection (server-side on private server)
2. **System prompt hardening** — explicit instructions in the private server LLM (Qwen) system prompt prohibiting instruction-following from retrieved content
3. **Response scanning** — scan private server LLM output for restricted/EC keyword matches before returning to client
4. **Audit log** — every restricted/EC session logs query hash, response hash, chunk source paths (no plaintext)

Full threat model, attack vectors, and implementation detail:
*Cross-reference: `spec-model-routing.md` §8 (Prompt Injection & EC Exfiltration Defense)*

---

## 9. Retrieval Query Design

The client queries two separate stores depending on routing tier:

```python
# Public store — local postgres, direct connection
public_store = RAGStore(settings.get("rag.database_url"))

# Restricted/EC store — private server postgres (Private-Server today), VPN required
ec_store = RAGStore(settings.get("rag.ec_database_url"))  # e.g., postgresql://private-server.example.org:5432/axiom_restricted
```

```python
def search(
    query_embedding: list[float] | None,
    query_text: str,
    access_tiers: list[str],          # from user auth: ["public"] or ["public", "export_controlled"]
    scopes: list[str] = ("community", "facility", "personal"),
    owner: str | None = None,         # user ID for personal scope filtering
    limit: int = 10,
) -> list[SearchResult]:
```

The WHERE clause (same SQL, different physical store):
```sql
WHERE access_tier = ANY(%(access_tiers)s)
  AND (
      scope = 'community'
      OR scope = 'facility'
      OR (scope = 'personal' AND owner = %(owner)s)
  )
```

Personal scope chunks are only returned when `owner` matches — no cross-user leakage.

`corpus_id` to `scope` mapping for the three-tier architecture:

| `corpus_id` | `scope` value in WHERE | Writable via |
|-------------|----------------------|--------------|
| `rag-community` | `'community'` | `axiom rag load-community` only (read-only at query time) |
| `rag-org` | `'facility'` | `axiom rag sync org` (admin) |
| `rag-internal` | `'personal'` | `axiom rag index` (user) |

`rag-community` is a read-only corpus at query time. No user-facing `axiom rag index` command writes to `scope = 'community'`. Community corpus updates come exclusively from `load-community` (which restores a versioned dump).

Add `rag.ec_database_url` to settings defaults (empty = EC RAG disabled):
```
axiom settings set rag.ec_database_url "postgresql://private-server.example.org:5432/axiom_ec"
```

---

## 10. Model-Agnostic Embedding

Embedding providers are declared in `models.toml` using the same provider pattern as
chat models, with `use_for = ["embedding"]`. The gateway selects the right provider
by task + routing tier — no code changes needed when switching embedding models.

```toml
[[gateway.providers]]
name         = "openai-embed"
endpoint     = "https://api.openai.com/v1"
model        = "text-embedding-3-small"
api_key_env  = "OPENAI_API_KEY"
use_for      = ["embedding"]
routing_tier = "public"
dims         = 1536

[[gateway.providers]]
name         = "nomic-local"
endpoint     = "http://localhost:11434"   # Ollama
model        = "nomic-embed-text"
use_for      = ["embedding"]
routing_tier = "export_controlled"        # used for EC content only
dims         = 768
```

`embed_texts(texts, access_tier)` calls `gateway._select_provider("embedding", access_tier)`,
so swapping the embedding model is a `models.toml` change — no Python changes needed.

**Routing profiles (v0.5.0):** Embedding providers will be selected via
routing profiles rather than flat `use_for` tags. See
[Model Routing Spec §10](spec-model-routing.md) for the design.

```toml
[routing_profiles.embedding]
providers = ["openai-embed", "ollama-embed"]
on_all_fail = "skip"            # Fall back to keyword search

[routing_profiles.ec_embedding]
providers = ["ollama-private-server"]   # Never cloud
on_all_fail = "error"           # EC content must be embedded locally
cloud_allowed = false
```

Each RAG corpus declares which embedding profile to use, ensuring EC
content is never sent to a cloud embedding API.

### 9.1 The Dimension Problem

Vector dimensions are fixed at index time. Mixing providers (e.g., 1536-dim public +
768-dim EC) means cross-tier similarity search is not meaningful — which is correct
behavior (EC queries should not retrieve public-embedded chunks with an EC query vector).

**Per-chunk dimension tracking:**

```sql
ALTER TABLE chunks ADD COLUMN embedding_model TEXT;   -- e.g., "text-embedding-3-small"
ALTER TABLE chunks ADD COLUMN embedding_dims  INT;    -- e.g., 1536
```

Query embedding must use the same `embedding_model` as the chunks being searched.
The retrieval layer reads the active embedding provider for the target `access_tier`
and embeds the query with the matching model.

**Re-indexing on model change:**

```bash
axiom rag reindex --tier public --model text-embedding-3-large   # upgrade public embeddings
axiom rag reindex --tier export_controlled                       # re-embed EC with current model
```

Only the `embedding` column changes; chunk text and metadata are preserved.

---

## 11. Intersection with Auth & Export Control Routing

The RAG access tier, LLM routing tier, and physical store location must all be consistent.
A restricted/EC query must use the private server LLM *and* the private server RAG store —
neither can be substituted without breaking the compliance boundary.

| User auth state | LLM routing | RAG retrieval | Physical store |
|-----------------|------------|---------------|----------------|
| No `restricted` / `export_controlled` role | `public` providers only | `access_tier = public` only | Local postgres |
| Has role, VPN connected | Both tiers | Both tiers | Local (public) + private server (restricted/EC) |
| Has role, VPN down | Public only + warning | Public only + warning | Local postgres only |

When a query routes to the restricted/EC tier (Qwen on Private-Server today), the RAG retrieval
searches `rag.ec_database_url` (private server postgres), not the local store. The query
embedding is also computed on the private server. The entire restricted/EC retrieval +
generation loop stays server-side.

*Cross-reference: `spec-model-routing.md` §7 (Auth intersection).*

---

## 12. Implementation Phases

### Phase Overview

| Phase | What | IAM required? |
|---|---|---|
| **0** | Local DuckDB store, pack install/list/remove, query fan-out (local + remote + PrivateCloud), PrivateCloud deployment | No |
| **1** | PostgreSQL schema migration (`access_tier` + `scope` columns), embedding fork, ingest auto-classification | No |
| **2** | Community corpus (`rag-community`), onboarding wizard integration, EC community content | Partial (pack entitlements deferred) |
| **3** | Personal RAG compounding: session history, signal pipeline, git logs, daily notes, watch daemon, TIDY stewardship | No (sync deferred to IAM) |
| **4** | Agentic RAG v1 (heuristic planner + evaluator) | No |
| **5** | Prompt caching | No |
| **6** | Agentic RAG v2 (LLM planner + evaluator) | No |

---

### Phase 0 — Local DuckDB Store + Pack Format (pre-IAM)

| Item | File | Status |
|------|------|--------|
| DuckDB local store at `~/.axi/rag/local.duckdb` | `rag/local_store.py` | 🔲 |
| `vss` extension install + `chunks` table schema | `rag/local_store.py` | 🔲 |
| `installed_packs` manifest table | `rag/local_store.py` | 🔲 |
| `axiom rag pack install <file.axiompack>` | `rag/cli.py` | 🔲 |
| `axiom rag pack list` / `axiom rag pack remove` | `rag/cli.py` | 🔲 |
| Query fan-out: local DuckDB + remote PostgreSQL + PrivateCloud | `rag/retriever.py` | 🔲 |
| Graceful degradation when remote unreachable | `rag/retriever.py` | 🔲 |
| Result merge by relevance score (corpus priority) | `rag/retriever.py` | 🔲 |
| `axiom rag export --personal` / `axiom rag import --personal` | `rag/cli.py` | 🔲 |

---

### Phase 1 — Schema + Embedding Fork (next)

| Item | File | Status |
|------|------|--------|
| Add `access_tier` + `scope` columns to schema | `rag/store.py` | 🔲 |
| Migrate `tier='institutional'` → `access_tier='public', scope='community'` | `rag/store.py` | 🔲 |
| Embed provider fallback (OpenAI → Ollama → skip) | `rag/embeddings.py` | ✅ v0.4.x |
| Adaptive rate limiter for embedding API | `infra/rate_limiter.py` | ✅ v0.4.x |
| Local embedding via Ollama (blocked by Ollama 0.18.x Metal bug) | `rag/embeddings.py` | ⚠ upstream |
| Ingest auto-classification using `router.py` | `rag/ingest.py` | 🔲 |
| Retrieval scope + tier filtering | `rag/store.py` | 🔲 |
| `axiom rag index` / `axiom rag status` CLI | `rag/cli.py` | 🔲 |

### Phase 2 — Community RAG + Onboarding

| Item | Notes |
|------|-------|
| Community knowledge base curation | regulatory body, DOE, IAEA public docs |
| `axiom rag sync community` command | Download + index versioned community content |
| Wizard onboarding integration | Prompt user to index personal docs during `axiom config` |
| EC community content | SimTool manuals (facility provides license; we provide ingest) |

### Phase 3 — Personal RAG Compounding

| Item | Notes | Status |
|------|-------|--------|
| Session history auto-indexing | `_schedule_session_index()` daemon thread after each turn | ✅ |
| Sense pipeline integration | `runtime/inbox/processed/` → `rag-internal` via `ingest_signals()` | ✅ |
| Git commit log indexing | Repos under `runtime/knowledge/` via `ingest_git_logs()` | ✅ |
| Daily notes (`axiom note`) | Timestamped daily markdown, immediate background index | ✅ |
| Filesystem watch (`axiom rag watch`) | `watchdog` observer, 2 s debounce, TIDY daemon supervised | ✅ |
| TIDY corpus lifecycle stewardship | Nightly index, session pruning, watch daemon install | 🔲 |
| Session TTL pruning | `store.delete_corpus_older_than()` + `rag.session_ttl_days` setting | 🔲 |
| Community RAG promotion pipeline | Personal → community with PII scrubbing + EC gating | 🔲 |
| Cross-scope relevance tuning | Weight community vs facility vs personal results | 🔲 |

### Phase 4 — Agentic RAG

| Item | Notes |
|------|-------|
| Query planner (heuristic) | Rule-based `RetrievalPlan`: skip retrieval, tier/scope selection, query reformulation |
| Context evaluator (heuristic) | `sufficient = best_score > 0.4 AND len(chunks) >= 3`; max 2 retrieval passes |
| Agentic loop integration | `QueryPlanner` + `ContextEvaluator` wired into `RAGStore.search()` call path |
| `interaction_log` integration | One record per agentic loop; `retrieval_plan` JSONB column; all-pass chunks captured |

*Full interaction_log schema: `spec-rag-knowledge-maturity.md`.*

### Phase 5 — Prompt Caching

| Item | Notes |
|------|-------|
| Gateway `_build_messages()` cache_control | Wrap `cache_hint = true` template blocks with `cache_control: {type: "ephemeral"}` |
| Prompt Template Registry integration | Templates declare `cache_hint`; gateway applies caching transparently |
| Cache hit metric | `axiom_llm_cache_hit_ratio` added to observability dashboard |

*Depends on prompt registry: `spec-prompt-registry.md`. Metrics: `spec-observability.md` §3.1.*

### Phase 6 — Agentic RAG v2

| Item | Notes |
|------|-------|
| LLM-based query planner | Cheapest available provider returns `RetrievalPlan` as structured JSON |
| LLM-based context evaluator | LLM evaluator call: "Is this context sufficient? If not, what is missing?" |

---

## 13. promptfoo Eval Harness

Axiom uses [promptfoo](https://promptfoo.dev) (MIT open-source) to evaluate RAG quality.
Configs live in `tests/promptfoo/`.

> **Note:** OpenAI acquired promptfoo on 2026-03-09. The MIT-licensed core continues and
> is what we use. Monitor for vendor lock-in; if necessary, fork or migrate.

### 12.1 Files

| File | Purpose |
|------|---------|
| `tests/promptfoo/promptfooconfig.yaml` | Chat agent quality + hallucination tests |
| `tests/promptfoo/rag-evals.yaml` | RAG retrieval relevance + grounding tests |
| `tests/promptfoo/redteam-export-control.yaml` | Adversarial EC safety sweep |
| `tests/promptfoo/rag_provider.py` | Python provider: calls `RAGStore.search()` → injects `{{RAG_CONTEXT}}` |

### 12.2 RAG Python Provider

`rag_provider.py` is a promptfoo Python provider that:
1. Accepts a `query` from test `vars`
2. Calls `RAGStore.search()` with appropriate `tier` and `scope`
3. Returns the retrieved chunks as `{{RAG_CONTEXT}}`
4. The downstream LLM provider uses `{{RAG_CONTEXT}}` in its system prompt

This lets promptfoo evaluate whether the LLM answer is grounded in *actually retrieved* content,
not just parametric knowledge.

### 12.3 Running Evals

```bash
cd tests/promptfoo

# Chat quality tests (uses Ollama judge — no API cost)
npx promptfoo eval -c promptfooconfig.yaml

# RAG retrieval + grounding tests (requires running PostgreSQL + RAG indexed)
npx promptfoo eval -c rag-evals.yaml

# Adversarial EC safety sweep (generates attack variants, tests refusals)
npx promptfoo redteam run -c redteam-export-control.yaml

# View results dashboard
npx promptfoo view
```

### 12.4 CI Integration

Add to the pre-push hook or CI pipeline:
```bash
# Fast chat quality check (Ollama judge, no API cost, ~60s)
npx promptfoo eval -c tests/promptfoo/promptfooconfig.yaml --ci
```

promptfoo returns exit code 1 if any assertions fail — compatible with standard CI gates.
Use `--cache` to avoid re-running identical prompts in repeated CI runs.

---

## 14. Agentic RAG

The sections above describe single-pass retrieval: receive query → embed → search →
inject chunks → generate. This section adds agentic (multi-step) retrieval, where the
system plans the query, evaluates the retrieved context, and decides whether to act or
fetch more before generating.

*Cross-reference: `spec-rag-knowledge-maturity.md` (interaction log schema and knowledge
maturity pipeline), `spec-prompt-registry.md` (prompt templates).*

### 14.1 What is Agentic RAG

**Single-pass RAG (current model):** receive query → embed → search → inject chunks →
generate. The agent has no opportunity to assess whether the retrieved context is
sufficient before generating.

**Agentic RAG** adds deliberation:

1. **Plan** — before retrieval, determine whether retrieval is needed and how to
   formulate the query.
2. **Retrieve** — execute the retrieval as planned.
3. **Evaluate** — assess whether the retrieved context is sufficient to answer accurately.
4. **Iterate** — if insufficient and a followup query is available, retrieve again
   (up to a configured maximum).
5. **Generate** — produce the response with the best available context.

This reduces hallucination on queries where initial retrieval is poor, and eliminates
unnecessary retrieval overhead for queries that don't need it (greetings, arithmetic,
clarification).

### 14.2 Query Planner

Before retrieval, a lightweight planning step determines:

- **Is retrieval necessary?** Greetings, arithmetic, simple clarification questions do
  not need retrieval.
- **Which tier(s) to search?** `public` only, or also `restricted`/`classified` if the
  user is authorized. (Axiom configures these as `public`, `restricted`,
  `export_controlled`.)
- **Which scope(s)?** `community` baseline always; add `facility` if the query is
  facility-specific; add `personal` if the query references personal context.
- **What query reformulation would improve recall?** Expand acronyms, add synonyms,
  decompose compound queries.

```python
@dataclass
class RetrievalPlan:
    needs_retrieval: bool
    query_text: str                  # possibly reformulated
    tiers: list[str]
    scopes: list[str]
    max_chunks: int
    rationale: str                   # logged for observability
```

**v1 — heuristic rules (configurable):**

- Skip retrieval if: `query_length < 15` chars, or query matches greeting/arithmetic patterns.
- Search classified tier only if: user has classified access AND query matches classified
  keyword signals.
- Include personal scope if: query contains first-person pronouns or references "my",
  "our", "I".

**v2 — LLM planner:** a small LLM call (cheapest available provider) that returns a
`RetrievalPlan` as structured JSON. See Phase 6 in §12.

### 14.3 Context Evaluator

After retrieval, before generation, evaluate whether the retrieved context is sufficient:

```python
@dataclass
class ContextEvaluation:
    sufficient: bool
    confidence: float           # 0.0–1.0
    missing_aspects: list[str]  # what's not covered
    suggested_followup_query: str | None
```

**v1 — heuristic:** `sufficient = best_score > 0.4 AND len(chunks) >= 3`

**v2 — LLM evaluator:** "Given this query and these retrieved chunks, is the context
sufficient to answer accurately? If not, what is missing?"

If `sufficient = False` and `suggested_followup_query` is not `None`: execute a second
retrieval with the followup query, merge results, re-evaluate. **Maximum 2 iterations**
to prevent runaway loops.

### 14.4 Agentic RAG Loop

```mermaid
flowchart TB
    Q[User query] --> P[Query Planner]
    P -->|needs_retrieval = false| G[Generate directly]
    P -->|needs_retrieval = true| R1[Retrieve — pass 1]
    R1 --> E1[Context Evaluator]
    E1 -->|sufficient| G
    E1 -->|insufficient, iteration < 2| R2[Retrieve — pass 2 — with followup query]
    R2 --> E2[Context Evaluator]
    E2 --> G
    G --> S[_scan_response] --> IL[Write interaction_log]
    IL --> Resp[Response to user]
```

### 14.5 Interaction Log Integration

Each agentic RAG loop writes one `interaction_log` record (see
`spec-rag-knowledge-maturity.md`). The `chunks_retrieved` field captures all chunks
from all passes. The `rationale` from the `RetrievalPlan` is stored in a new
`retrieval_plan` JSONB column added to the `interaction_log` table.

### 14.6 Configuration

```toml
[rag.agentic]
enabled              = true
max_retrieval_passes = 2
planner_mode         = "heuristic"   # heuristic | llm (v2)
evaluator_mode       = "heuristic"   # heuristic | llm (v2)
evaluator_threshold  = 0.4           # min best_score to consider context sufficient
```

### 14.7 EC Compliance in Agentic RAG

The query planner and context evaluator themselves must not see export-controlled
content — they run on the client side with public-tier context only. Retrieval steps
are tier-gated as in the single-pass model.

The followup query (generated by the context evaluator) is derived from the evaluator's
assessment of the public-tier context, not from EC chunks. If the user has restricted/classified
access, a separate restricted/EC retrieval pass runs on the private server (Private-Server today;
PrivateCloud for classified content when available) as before — the planner and evaluator have no
visibility into that path.

---

## 15. Prompt Caching

### 15.1 Why Prompt Caching Matters

Static prompt blocks — system preambles, persona definitions, tool descriptions,
few-shot examples — are re-sent on every completion request but rarely change between
turns or even between sessions. Anthropic supports `cache_control: {type: "ephemeral"}`
on message blocks, caching them server-side for up to 5 minutes. For active users and
agentic RAG loops (which may make multiple LLM calls per user query), this is a
material cost and latency reduction.

### 15.2 What Gets Cached

| Block | Static? | Cache? |
|-------|---------|--------|
| EC hardened preamble | Yes | Yes — template `ec_hardened_preamble`, `cache_hint = true` |
| Agent persona system prompt | Yes | Yes — each agent's persona template |
| Tool descriptions | Yes (per session) | Yes |
| Few-shot examples | Yes (per template version) | Yes |
| RAG context (retrieved chunks) | No — changes per query | No |
| User message | No | No |

### 15.3 Message Ordering for Cache Efficiency

Anthropic's cache activates only when cached blocks appear before uncached blocks in
the message array. The gateway orders message blocks as follows:

```
[cached system blocks]
→ [cached few-shot examples]
→ [dynamic RAG context]
→ [user message]
```

This ordering maximises cache hits even as the dynamic RAG context and user message
change on every turn.

### 15.4 Implementation

Prompt caching is handled via the Prompt Template Registry
(`spec-prompt-registry.md`). Templates with `cache_hint = true` are wrapped with
`cache_control` by the gateway's `_build_messages()` method. No changes are required
at the agent level — agents declare their templates as usual; caching is applied
transparently by the gateway.

Cache hit metrics are surfaced as `axiom_llm_cache_hit_ratio`
(see `spec-observability.md` §3.1).
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
