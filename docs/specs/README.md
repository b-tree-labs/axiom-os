# Technical Specifications

This folder contains technical specifications, design documents, and research for Axiom.

**PRDs define *what* to build. Specs define *how* to build it.**

## Document Structure

```
specs/
├── spec-executive.md    # Full technical architecture
├── spec-executive.md   # 2-page technical overview
├── design-prompts/                   # Implementation guides for AI/dev
├── diagrams/                         # Architecture diagrams
├── platform-comparison-databricks.md # Platform alternatives analysis
└── hyperledger-domain-specific-use-cases.md  # Research: blockchain applications
```

## Core Specifications

| Document | Purpose | Audience |
|----------|---------|----------|
| [Master Tech Spec](spec-executive.md) | Complete architecture, schemas, APIs | Engineers |
| [Executive Tech Spec](spec-executive.md) | 2-page technical overview | Stakeholders, PMs |

## Memory subsystem — read this stack first

Axiom Memory has a single normative contract every extension consumes. Read in this order:

| Order | Document | What it gives you |
|---|---|---|
| 1 | [`prd-memory.md`](../prds/prd-memory.md) | The product surface — outcomes, personas, distinctive bets, success metrics |
| 2 | [`spec-memory.md`](spec-memory.md) | **Authoritative normative contract** — every memorable read/write, every layer, the compliance checklist |
| 3 | [`spec-federation-policy.md`](spec-federation-policy.md) | VisibilityHorizon + ClassificationStamp + TrustProfile + FederationGateway primitives |
| 4 | [`spec-classification-boundary.md`](spec-classification-boundary.md) | Regulatory regimes (CUI / EAR / ITAR / Part 810) consumed by the federation gateway |
| 5 | [ADR-033](../adrs/adr-033-layered-memory-architecture.md) | The four-layer architecture commitment + migration plan |
| 6 | [`working/memory-benchmarks.md`](../working/memory-benchmarks.md) | Compliance suite + performance baseline + public benchmark plan |

**Subordinate to spec-memory** (one source of truth for graph + sessions + state):

- [`spec-knowledge-graph.md`](spec-knowledge-graph.md) — one backend impl behind the L2 ConceptGraph protocol; AGE-on-Postgres for Server tier
- [`spec-session-store.md`](spec-session-store.md) — read-cache for L1 conversation_turn fragments; not the source of truth
- [`spec-agent-state-management.md`](spec-agent-state-management.md) — operational state only (cursors, presence, locks); cognitive state goes through MemoryStore

When this stack and any other doc disagree, **the memory stack wins**. Future agents and extension authors: skim the order above, write against `spec-memory.md`, you get rich, fully-featured memory by default.

## Design Prompts

The `design-prompts/` folder contains implementation guides:

| Prompt | Component |
|--------|-----------|
| [Bronze Layer Ingest](design-prompts/prompt-bronze-layer-ingest.md) | Dagster + Iceberg ingestion |
| [dbt Silver Models](design-prompts/prompt-dbt-silver-models.md) | Data transformation |
| [Superset Dashboards](design-prompts/prompt-superset-dashboards.md) | Analytics visualizations |
| [Dagster Orchestration](design-prompts/prompt-dagster-orchestration.md) | Pipeline scheduling |

## Research & Analysis

| Document | Topic |
|----------|-------|
| [Platform Comparison](platform-comparison-databricks.md) | Databricks/Snowflake alternatives |
| [Hyperledger Use Cases](hyperledger-domain-specific-use-cases.md) | Blockchain for domain-specific |

## Proposals & External Docs

| Document | Purpose |
|----------|---------|
| [CINR Pre-App](CINR_PreApp_Concept_Draft.md) | Grant pre-application |
| [LDRD Collaboration](LDRD_Collaboration_OnePager.md) | INL partnership proposal |

## Relationship to PRDs

| PRD (what) | Spec (how) |
|------------|------------|
| [Executive PRD](../prd/axiom-executive-prd.md) | [Master Tech Spec](spec-executive.md) |
| [System Ops Log PRD](../prd/system-ops-log-prd.md) | Tech spec §3.4.5 (log_entries schema) |
| [Experiment Manager PRD](../prd/experiment-manager-prd.md) | Tech spec §3.4.6 (sample_tracking schema) |
| [Analytics PRD](../prd/analytics-dashboards-prd.md) | [Superset Design Prompt](design-prompts/prompt-superset-dashboards.md) |

## Word Documents

Word (.docx) versions are maintained for stakeholder review:
- `axiom-master-tech-spec.docx`
- `axiom-executive-summary.docx`
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
