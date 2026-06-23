# Data Architecture Specification

**Part of:** [Axiom Master Tech Spec](spec-executive.md)

---

> **Scope:** This document specifies the Axiom data architecture, including the medallion pattern, Apache Iceberg configuration, schema definitions, data quality framework, streaming readiness, and operational policies (backup, retention, archive, DR). Domain-specific schemas (e.g., nuclear reactor data) are defined by downstream products.

| Property | Value |
|----------|-------|
| Version | 0.4 |
| Last Updated | 2026-05-28 |
| Status | Draft |

---

## Architecture update — 2026-05-28 (read this first)

§§2–9 define the medallion lakehouse design (Iceberg/DuckDB/dbt/Dagster). That
**lakehouse engine is not built** ("schema sketched, not wired"). The platform is
being grown *from the ingest side first*, via small shippable initiatives. Current
thinking on shape, packaging, and the hard problems:

### Layering — substrate vs orchestration vs served (the load-bearing model)

Three layers, distinct responsibilities — do not conflate them:

- **Data-platform = substrate of record.** The medallion lakehouse (Iceberg
  tables bronze→silver→gold + the metadata catalog, §§2–9) is where data and
  metadata *live*. It is the durable system of record.
- **Sense = thin async orchestration on top.** The `signals`/Sense subsystem is
  the *orchestration* layer — sources, scheduling, the work queue, async
  processing/dispatch. It *moves* data into the substrate; it does **not** hold
  the system of record (its inbox is a working area, not the medallion).
- **RAG (pgvector) = a served view.** Retrieval is a consumer projection fed
  from silver/gold — not a system of record.

**Orchestration boundary (ADR-049).** Dagster, Sense, and dbt all "poll →
detect → enqueue → schedule," which *looks* like duplication. It is not — there
is **one orchestrator per pipeline, chosen by destination** (a single binary
question: does this data become a lakehouse asset, or is it an ephemeral agent
signal?):

- data that becomes a **lakehouse asset → Dagster** — sensors / schedules /
  assets; the only thing that writes Iceberg + runs dbt;
- an **ephemeral agent signal → Sense** — the agent-fleet subsystem, orthogonal
  to the data platform; **not** a data-pipeline orchestrator.

These never overlap for the same data: it either has a durable table home
(Dagster) or it does not (Sense). **`IngestSource` is a portable *connector*
contract, not an orchestrator** — a connector (Box, …) is written once
(`list_changed`/`fetch`) and consumed by a Dagster sensor in the heavy tier or a
minimal runner on a lean/offline node; **no deployment runs both for the same
data**, which is what *removes* duplication (the connector logic is written
once; the orchestrators are tier-specific runtimes). Use Dagster/dbt **native**
source primitives inside the lakehouse; the connector seam is only for sources
lacking a native connector. (Box auth/session reuses the `publishing` Box
provider; a pull-oriented Box client is the genuinely new piece.) See **ADR-049**
for the full rationale, including why neither orchestrator subsumes the other.

**Medallion depth = full.** Pipeline #1 stands up the **full** medallion
(Iceberg/DuckDB/dbt gold), not a RAG-only served layer — the lakehouse is the
substrate from the start, with the RAG served view fed from it.

### Deployment topology + IaC (generic; facility mapping is downstream)

The platform deploys as **reproducible infrastructure (Terraform modules)** so
one definition stands up at multiple sites with different postures: **acquisition**
sites (raw capture + pre-process, then push curated content outward) and
**processing/served** sites (downstream monitoring, analytics, served views).
Site, classification posture, and runtime services are **parameters** — the
platform names no specific facility or export-control tier; that mapping is a
downstream-product concern.

### Packaging & topology — the `data_platform` extension + agent orchestrator
- The data platform is an **Axiom builtin extension** (`extensions/builtins/data_platform/`), **not core** — its heavy toolchain (Iceberg/Dagster/dbt/DuckDB) lives behind a `pip install axiom-os-lm[data-platform]` optional extra, so base installs stay lean. **Extract to its own package/repo on a trigger** (dependency/cadence divergence or product-ization), per the portfolio pattern.
- It ships an **agent orchestrator** (the data-platform agent) that owns: source monitoring, scheduled ingest (through the governance gate), pack generation + distribution, and corpus/lakehouse health.
- **Consumer layers extend it by registration, not forking:** a domain extension registers `IngestSource` / `SchemaPack` / `TransformPack` / governance-rule packs into the platform registry — the same generic-mechanism + domain-config pattern proven by `rag.ingest_router` + a consumer's rule files.

### Ingestion governance (BUILT — v0.22.0) — the first real piece
- durable, resumable ingest (`axi rag ingest`: preflight, checkpoint/resume, calibration, progress) — see `spec-rag-ingest-advanced.md`;
- a **provenance/artifact gate** (`rag.ingest_router`) that excludes/quarantines controlled or proprietary content **by source + artifact type, before it is read** — provenance-based, not keyword-based;
- **safe-by-default**: shared-tier ingest refuses without a rule set;
- **`axi rag audit`** to find/purge controlled content already in a corpus;
- honest per-reason drop reporting + embed-failure durability.

This gate is **mandatory in every automated ingest path** below.

### Pack distribution at scale (the hard problem)
A large shared corpus **must not be replicated wholesale** to every node that wakes up. Split by size and need:
- **Big shared corpus → federated, not packed.** It stays on the shared node(s); waking nodes query it **live, local-first** (return local immediately, fold shared results within a tight deadline, cache hot chunks). The bulk never transfers.
- **Packs become deltas + scopes.** The `corpus_generation` the store already tracks lets a node pull only the **diff since its last generation**, and only the **scope it needs** (tenant/topic) — never the whole corpus.
- **Shrink what transfers.** Pack size is dominated by embeddings (768-dim float32 ≈ 4× the chunk text): **quantize to int8** (~4× smaller), or ship **text-only packs + re-embed locally** on nodes with an embedder.
- **Retrieval quality at size** (separate problem): scope/metadata-filtered retrieval first, a re-rank pass over candidates, and the semantic-chunk generation system keep top-k clean as the corpus grows.

### Scheduled source pipelines (next initiative)
The data-platform agent runs **scheduled source monitors** that poll external endpoints (e.g. a document store), pull changed content on a schedule, run it **through the governance gate**, ingest it, and regenerate/flow packs — keeping the corpus current without manual hassle. The scheduler uses the host-install schedule infra (launchd/systemd/cron fallback). The gate is non-negotiable here: an unattended pull is exactly where controlled content would leak in.

### Seamless query contract (consumer-facing; detail in `spec-rag-architecture`)
The platform exists to serve a **seam-free chat**: a locally-initiated prompt uses the **local RAG + local LLM** first, enriched by the shared corpus via a **local-first federated fan-out that never blocks on the network**, with **one-question connect** ("Use the &lt;community&gt; RAG + LLM &lt;name&gt;? [Y/n]") instead of manual endpoint configuration. The data platform's job is keeping content current and packs flowing.

### Status
| Piece | Status |
|---|---|
| Ingest + provenance gate + audit | ✅ built (v0.22.0) |
| Layering + source reconciliation (Sense=orchestration, platform=substrate, RAG=served) | ✅ decided 2026-05-28 (above) |
| `data_platform` extension + agent orchestrator (PLINTH) | 🟡 scaffolding |
| **Pipeline #1** — scheduled source → **full medallion** → RAG served (Sense-orchestrated, gate-enforced) | 🔲 next (phase 1) |
| Medallion lakehouse (Iceberg/DuckDB/dbt) — full, per the depth decision | 🔲 phase 1 (§§2–9 below) |
| Terraform IaC (multi-site reproducible: acquisition / processing-served) | 🔲 phase 1 |
| PLINTH initial agent skills + capabilities | 🔲 to design |
| Delta/scoped/quantized packs + hot-chunk cache | 🔲 designed here |
| Seamless federated chat (local-first, one-question connect) | 🟡 primitives built, wiring pending |

---

## Table of Contents

1. [Overview](#1-overview)
2. [Medallion Architecture](#2-medallion-architecture)
3. [Layer Specifications](#3-layer-specifications)
4. [Gold Layer Schemas](#4-gold-layer-schemas)
5. [Data Quality Framework](#5-data-quality-framework)
6. [Apache Iceberg Configuration](#6-apache-iceberg-configuration)
7. [Platform Comparison](#7-platform-comparison)
8. [Streaming Architecture](#8-streaming-architecture)
9. [Backup, Retention & Archive Policy](#9-backup-retention--archive-policy)

---

## 1. Overview

Axiom employs a medallion architecture (Bronze → Silver → Gold) built on:

- **Apache Iceberg** for time-travel capabilities and schema evolution
- **DuckDB** as the query engine
- **dbt** for transformations
- **Dagster** for orchestration

---

## 2. Medallion Architecture

### Layer Characteristics

| Layer | Purpose | Mutability | Typical Format |
|-------|---------|------------|----------------|
| **Bronze** | Raw ingestion | Append-only | Parquet (Iceberg) |
| **Silver** | Cleaned, validated | Upsert | Parquet (Iceberg) |
| **Gold** | Aggregated, business-ready | Materialized views | Parquet (Iceberg) |

---

## 3. Layer Specifications

### 3.1 Bronze Layer

Raw, unprocessed data exactly as received. Append-only to preserve complete history.

| Table | Source | Grain | Partitioning |
|-------|--------|-------|--------------|
| `interaction_log_raw` | RAG completions | Per completion | `date`, `tenant_id` |
| `agent_state_raw` | Agent state transitions | Per transition | `date`, `tenant_id` |

> **Note:** Domain-specific Bronze tables (e.g., sensor readings, operations logs, simulation outputs) are defined by downstream products. See [Data Platform PRD](../requirements/prd-data-platform.md) for the generic framework; axiom defines nuclear-specific Bronze schemas in its own Data Platform PRD.

**Multi-Tenant:** All tables partitioned by `org_id` and `system_id` for tenant isolation.

### 3.2 Silver Layer

Cleaned, validated, and deduplicated data. dbt transformations apply business rules.

| Table | Source | Transformations |
|-------|--------|-----------------|
| `interaction_log` | Bronze | Dedup, session linking, schema enforcement |
| `agent_state_transitions` | Bronze | FK validation, schema enforcement |

> **Note:** Domain-specific Silver tables are defined by downstream products.

### 3.3 Gold Layer

Business-ready, aggregated datasets optimized for analytics and dashboards.

| Table | Grain | Use Case |
|-------|-------|----------|
| `system_hourly_metrics` | Hour | Dashboard KPIs |
| `operational_kpis` | Day | Management reporting |
| `compliance_summary` | Day | Regulatory reporting |
| `interaction_analytics` | Day | RAG usage metrics |

> **Note:** Domain-specific Gold tables (e.g., fuel burnup, xenon dynamics for nuclear facilities) are defined by downstream product specs, not here.

---

## 4. Gold Layer Schemas

### 4.1 system_hourly_metrics

| Column | Type | Description |
|--------|------|-------------|
| `system_id` | string | System identifier |
| `hour` | timestamp | Hour bucket |
| `metrics` | jsonb | Domain-specific metric payload |
| `source` | enum | `measured` \| `modeled` |

> **Note:** The `metrics` JSONB column contains domain-specific fields. Downstream products define the schema (e.g., axiom defines `avg_power_kw`, `max_fuel_temp_c`).

### 4.2 log_entries (Unified Log)

Single table with `entry_type` discriminator for all log types.

| Column | Type | Description |
|--------|------|-------------|
| `entry_id` | uuid | Primary key |
| `system_id` | string | System identifier |
| `timestamp` | timestamp | Entry time |
| `entry_type` | string | Extensible type discriminator |
| `operator_id` | string | User who created |
| `content` | jsonb | Type-specific payload |

**Built-in Entry Types:**

| Type | Description |
|------|-------------|
| `status_check` | Periodic system status check |
| `startup` | System startup |
| `shutdown` | Normal shutdown |
| `emergency_stop` | Emergency shutdown |
| `maintenance` | Equipment issues |
| `general_note` | Miscellaneous |

> Domain-specific entry types (e.g., `radiation_survey`, `experiment_log`, `console_check` for nuclear) are registered by downstream products via extension configuration.

### 4.3 Domain-Specific Tables

Domain-specific Bronze/Silver/Gold tables (e.g., digital twin run tracking, simulation outputs, domain sensor data) are defined by downstream products, not in this spec. Axiom provides the Iceberg/dbt/Dagster framework; downstream products register their schemas via the extension system.

See [Data Platform PRD § Domain Extension Points](../requirements/prd-data-platform.md#domain-extension-points) for the extension model.

---

## 5. Data Quality Framework

### Quality Tests

| Test Type | Layer | Example |
|-----------|-------|---------|
| **Not null** | Bronze | `sensor_id IS NOT NULL` |
| **Unique** | Silver | `entry_id` unique per table |
| **Referential** | Silver | `system_id` exists in `systems` |
| **Range** | Silver | Domain-specific value bounds |
| **Freshness** | Gold | Data < 1 hour old |
| **Custom** | Gold | Domain-specific consistency checks |

---

## 6. Apache Iceberg Configuration

### 6.1 Catalog Configuration

| Setting | Value | Rationale |
|---------|-------|-----------|
| Catalog type | REST | Standard API for multi-engine access |
| Metadata location | S3 (Ceph/Rook) | Durable, shared storage |
| Warehouse | `s3://axiom-lakehouse/` | All Iceberg data |

### 6.2 Partitioning Strategy

| Table Pattern | Partition Columns | Rationale |
|---------------|-------------------|-----------|
| Sensor data | `date`, `system_id` | Query by time + tenant |
| Logs | `date`, `entry_type` | Query by time + type |
| Simulations | `date`, `model_type` | Query by time + model |

### 6.3 Key Capabilities

- **Time-travel queries:** Query data as it existed at any point
- **Schema evolution:** Add/rename/drop columns without rewriting
- **Partition evolution:** Change partitioning without data movement
- **ACID transactions:** Concurrent reads and writes

---

## 7. Platform Comparison

### 7.1 Decision Summary

We chose open-source (Iceberg + DuckDB + dbt) over commercial platforms (Databricks, Snowflake).

### 7.2 Decision Rationale

| Factor | Open Lakehouse | Commercial Platform |
|--------|----------------|---------------------|
| **Cost** | Fixed infrastructure | Per-compute pricing |
| **Data sovereignty** | Full control | Vendor access |
| **Domain integration** | Native Python/HDF5 | Limited |
| **On-premise** | Supported | Cloud-primary |
| **Research integrity** | Open pipelines | Proprietary |
| **Workforce dev** | Industry-standard tools | Vendor-specific |

**Migration path:** Open formats (Iceberg, Parquet) ensure future migration feasibility.

### 7.3 INL DeepLynx Partnership Opportunity

**Status:** Exploratory — non-committal technical alignment

Idaho National Laboratory's [DeepLynx Nexus](https://github.com/idaholab/DeepLynx) is an open-source (MIT) digital engineering backbone developed for domain-specific projects. After codebase analysis, we've identified significant technology overlap and complementary capabilities.

**Technology Overlap:**
- Both use **DuckDB** for timeseries analytics
- Both target **domain-specific digital twins**
- Both provide **MCP servers** for AI agent integration

**Complementary Strengths:**

| Capability | DeepLynx | Axiom |
|------------|----------|-------|
| Ontology management | ✅ Mature (Class/Relationship model) | ⚠️ YAML schemas |
| Graph traversal | ✅ Native | ⚠️ Via JOINs |
| Real-time streaming | ⚠️ Batch webhooks | ✅ Kafka |
| Time-series analytics | ✅ DuckDB | ✅ DuckDB + Iceberg |
| ML/ROM workflows | ⚠️ Not focus | ✅ Native |
| AI agent tooling | ⚠️ Basic MCP | ✅ Full MCP |

**Potential Integration Approaches:**

1. **Data Exchange** (Low commitment): CSV/Parquet interchange for timeseries data
2. **MCP Interoperability** (Medium commitment): AI agents access both systems via unified tool spec
3. **Ontology Alignment** (Medium commitment): Share system ontology vocabulary
4. **Plugin Architecture** (High commitment): DeepLynx as optional ConfigurationPlugin for Axiom

**Reference:** Full technical analysis in [docs/research/deeplynx-assessment.md](../research/deeplynx-assessment.md)

---

## 8. Streaming Architecture

> **See:** [ADR-007: Streaming-First Architecture](../adrs/adr-007-streaming-first-architecture.md)

### 8.1 Design Principle

Build for streaming; use batch as fallback.

### 8.2 Event Schema

All events follow a common envelope:

```json
{
  "event_id": "uuid",
  "event_type": "sensor_reading | prediction | log_entry",
  "timestamp": "ISO8601",
  "source": { "tenant": "tenant-id", "system": "system-id" },
  "payload": { ... },
  "metadata": { "schema_version": "1.0" }
}
```

### 8.3 Latency Targets

| Data Type | Target Latency | Streaming Tech |
|-----------|----------------|----------------|
| Sensor readings | <1s | Kafka |
| Log entries | <5s | Kafka |
| Predictions | <100ms | Direct API |
| Aggregations | <1 min | Flink/Materialize |

---

## 9. Backup, Retention & Archive Policy

All data in the lakehouse must support configurable retention requirements and disaster recovery. This is the single canonical definition of operational policies for the Axiom data stack. Downstream products extend these policies for domain-specific regulatory requirements.

### 9.1 Retention Tiers

| Tier | Retention | Data Types | Storage |
|------|-----------|------------|---------|
| **Hot** | 90 days | Live sensor data, recent logs | Primary object storage (Ceph/Rook) |
| **Warm** | 2 years | Historical operations, default regulatory window | Primary object storage |
| **Cold** | 7 years | Audit trails, regulatory records (opt-in) | Glacier-tier / archive storage |
| **Archive** | Indefinite | Safety basis, licensing documents (opt-in) | Glacier-tier / offline |

**Configuration:** Retention tier is set per deployment via `[retention] policy` in `data-platform.toml`:
- `"standard"` (default): Hot + Warm tiers only (2-year max)
- `"regulatory"`: All four tiers active (7-year Cold + indefinite Archive)

Downstream products may define additional policy values for domain-specific requirements.

### 9.2 Backup Strategy

| Component | Frequency | Destination | Retention |
|-----------|-----------|-------------|-----------|
| PostgreSQL | Daily | S3 (Ceph/Rook) + offsite | 90 days |
| Iceberg metadata | Continuous | S3 | With data |
| Iceberg data | Via Iceberg | S3 | Per tier |
| Configuration | On change | Git + S3 | Indefinite |
| System logs | Daily | S3 + Glacier (if regulatory) | Per tier |
| Audit logs | Daily | S3 + Glacier (if regulatory) | Per tier |

### 9.3 Disaster Recovery

| Scenario | RTO | RPO | Recovery Method |
|----------|-----|-----|-----------------|
| Single node failure | <1 hour | 0 | K8s pod restart |
| Database corruption | <4 hours | <1 hour | Restore from backup |
| Full site loss | <24 hours | <1 day | Offsite restore |

### 9.4 Regulatory Compliance Extensions

The base Axiom policy provides the retention framework. Downstream products activate regulatory tiers as needed:

- **2-year warm tier:** Default for all deployments. Sufficient for most operational needs.
- **7-year cold tier:** Activated by `policy = "regulatory"`. Required for facilities subject to regulatory audit trails.
- **Indefinite archive:** Activated by `policy = "regulatory"`. For safety basis documents and licensing records.
- **Immutability:** All backups are append-only; no modification or deletion allowed.
- **Versioning:** Iceberg time-travel enables recovery of any point-in-time data within the retention window.
- **Audit trail:** All backup operations logged with tamper-evident verification (Hyperledger or HMAC chain, per deployment configuration).

### 9.5 Encryption

| Layer | Encryption | Key Management |
|-------|------------|----------------|
| At rest | AES-256 | OpenBao (production) / OS Keychain (dev) |
| In transit | TLS 1.3 | Auto-renewed certificates (cert-manager) |
| Backups | AES-256 | Separate backup keys |

**Key Rotation:** Quarterly for active encryption keys; archived keys retained for the lifetime of the data they protect.

---

## Related Documents

- [Executive Spec](spec-executive.md) — Master tech spec (§ Infrastructure Services for service inventory)
- [Model Corral Spec](spec-model-corral.md) — Model registry
- [Digital Twin Hosting Spec](spec-digital-twin-architecture.md) — DT execution
- [ADR-003: Lakehouse Architecture](../adrs/adr-003-lakehouse-iceberg-duckdb-superset.md) — Decision record
- [ADR-007: Streaming-First](../adrs/adr-007-streaming-first-architecture.md) — Decision record
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
