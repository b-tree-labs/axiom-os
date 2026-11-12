# ADR-019: Node Profiles — Edge / Workstation / Server / Platform

**Status:** Accepted  
**Date:** 2026-04-13  
**Deciders:** Benjamin Booth  
**Related:** ADR-013 (self-hosted node K3D + containerd), ADR-018 (self-hosted node public endpoint), PRD Managed Infrastructure, Memory: Tier 0 infra graceful degradation

---

## Context

Axiom has accumulated multiple tier vocabularies that risk collision:
- **CLI tiers** (`infra/cli_tiers.py`) — progressive disclosure of commands
- **RAG tiers** (ADR-014) — storage strategies (`rag-internal`, `rag-org`, `rag-community`, `rag-export-controlled`)
- **Tier 0 infra** (memory, 2026-04-02) — deployment graceful degradation (K3D → Docker Compose → native)

None of these address a fourth concern: **what hardware profile is this node, and what should be installed on it?** A self-hosted node is currently deployed with the full data/LLM stack but isn't framed as a named profile. The same applies to the planned HPC-cluster deployment.

Further, domain consumers keep showing up as "special node types" (a self-hosted node runs the domain consumer + Qwen + K3D + PG + ...) rather than as extensions on top of a standard Axiom profile — an architectural inconsistency flagged during Phase 0 planning.

## Decision

Introduce **Node Profiles** — a fourth dimension, orthogonal to CLI tiers, RAG tiers, and deployment mode. Profiles describe hardware-driven capability envelopes and the installed components that match them.

| Profile | Target Hardware | What's Installed | Example |
|---------|----------------|------------------|---------|
| **Edge** | Laptop-class (8-32GB RAM, optional consumer GPU, 100GB disk) | PostgreSQL + pgvector, llamafile with Bonsai 1.7B, personal RAG, ArtifactRegistry. No data platform. | Student laptop, dev machine |
| **Workstation** | Desktop-class (64-128GB RAM, RTX-class GPU, 1TB+ disk) | Edge + medium LLM (7B-13B), SeaweedFS, DuckDB. No Dagster orchestration. | Solo researcher, small lab |
| **Server** | Single-rack (128-512GB RAM, 24-97GB GPU, 10TB+ disk, single-node) | Workstation + Apache Iceberg, Dagster, Open WebUI, Langfuse, large LLM (70B+). | **A self-hosted node (current)**, classroom host |
| **Platform** | Datacenter (multi-node, HA, 1TB+ RAM aggregate, 80GB+ GPU, 20TB+ storage) | Server + HA PostgreSQL, replicated SeaweedFS, federation coordinator role, multi-tenant isolation. | **A shared HPC cluster (planned)**, institution-scale |

### Principles

1. **Profiles are Axiom-level infrastructure** (`axiom/src/axiom/infra/profiles/`). Domain consumers (the domain consumer, classroom) run on any profile that meets their declared requirements; they don't define profiles.
2. **Profile is detected + recommended at install time.** `axi config` uses existing `setup/probe.py` (RAM/GPU/disk/Docker detection) to recommend a profile. User confirms or overrides.
3. **Extensions declare capability requirements** (per PRD Managed Infrastructure): `[requires.llm context_window=4096]`, `[requires.data_platform medallion=true]`. Installer resolves against profile; refuses install if requirements exceed profile capabilities.
4. **Promotion and demotion are first-class.** `axi infra promote --to workstation` checks hardware, installs delta. `axi infra demote` tears down components no longer needed. No data loss during tier changes.
5. **Server vs. Platform is single-node vs. multi-node HA**, not just bigger hardware. A Server is one machine; a Platform is redundant infrastructure designed to be authoritative for long-lived state.

### Wrapping Existing Deployments

- **A self-hosted node** becomes a named Server-profile deployment. Qwen 122B on llama-server is a managed component (provider `qwen-node-server`), not an ad-hoc host process. Existing behavior unchanged; it's declaratively managed now.
- **A shared HPC cluster** (planned) becomes a Platform-profile deployment.
- **Bonsai LLM** on Edge/Workstation is unchanged (already an Axiom-managed component).

## Consequences

**Positive:**
- Single coherent story for "what does an Axiom node look like?" at any hardware scale
- Installer can make intelligent recommendations instead of a generic flow
- Domain consumers (the domain consumer, classroom) become extensions over standard profiles — architectural consistency restored
- Clear upgrade path for users outgrowing their current profile
- Supports the Phase 0 integrated plan (classroom + data platform on same deployment)

**Negative:**
- Installer becomes more complex (capability resolution, promotion/demotion logic)
- More components in the default Server profile = heavier deployment than historical Axiom

**Neutral:**
- Profiles don't replace CLI tiers, RAG tiers, or deployment modes — they compose with them. A Server-profile node running in K3D mode offers CLI Tier 2 commands to users with RAG-org-tier access. Four independent axes.

## Open Questions

Deferred to implementation:
- Exact capability declaration schema for extensions
- Promotion/demotion algorithm when components have runtime state
- How multi-context workspaces (ADR-020) share installed components within a profile

---

*See also: ADR-020 Federation Identity & Relationships, ADR-021 Federation Threat Model.*
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
