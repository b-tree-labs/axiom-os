# ADR-015: Shared Service Boundaries Between Axiom and Domain Extensions

**Status:** Accepted
**Date:** 2026-03-31
**Authors:** Benjamin Booth, Claude

## Context

Axiom is a domain-agnostic framework. Domain-specific extension layers (e.g., a
hypothetical "FacilityOS" for industrial facility management) depend on shared
infrastructure (PostgreSQL, LLM providers, object storage, observability) but must
remain independently deployable and evolvable.

This ADR codifies the ownership model for shared services and the contract between
axiom (framework) and any domain extension layer built on top of it.

## Decision

### Adopt First, Provision Second

The axiom installer (`axi infra`) MUST prefer discovering and reusing existing
shared resources over provisioning new instances. For every managed dependency
(PostgreSQL, LLM runtime, object storage), the installer follows this sequence:

1. **Discover** — probe for existing instances (localhost, local network, config)
2. **Verify** — check against minimum operating criteria defined by axiom
3. **Upgrade** — if below minimum but fixable, request permission, upgrade non-destructively
4. **Provision** — only if nothing suitable found, install fresh

The installer MUST have or request sufficient permissions to verify and upgrade
existing resources (e.g., `CREATE EXTENSION` on PostgreSQL, model pull on Ollama).
If permissions are insufficient, it reports the specific gap and remediation steps
rather than failing silently or forcing a parallel install.

Domain extension layers may raise minimum criteria (e.g., require a larger model
or additional PostgreSQL extensions) but may never lower axiom's floors.

### Ownership Model

Every shared resource has exactly **one owner** and **zero or more consumers**.
The owner controls schema, lifecycle, and configuration. Consumers connect via
environment variables and must tolerate the resource being absent (graceful degradation).

| Resource | Owner | Consumers | Connection Contract |
|----------|-------|-----------|-------------------|
| PostgreSQL server | Infrastructure (Helm/Terraform) | axiom, domain extensions | `*_DB_URL` env var per database |
| axiom database (`axiom_db`) | axiom (Alembic migrations) | axiom extensions only | `AXIOM_DB_URL` |
| Domain extension database | Domain layer (its own Alembic migrations) | Domain extensions only | Domain-specific env var (e.g., `FACILITY_DB_URL`) |
| LLM gateway | axiom (`axiom.infra.gateway`) | All extensions | `llm-providers.toml` + provider identity |
| LLM runtime (local SLM/LLM) | axiom (`axiom.infra.llm_runtime`) | axiom gateway | Managed by axiom installer; BYOI fallback via `localEndpoint` |
| Object storage (SeaweedFS/S3) | Infrastructure (Helm/Terraform) | RAG pack server, media library | `AXIOM_S3_ENDPOINT` env var |
| Keystore | axiom (`axiom.infra.keystore`) | All services | K8s Secrets API; cloud SM/KV via CSI driver |
| Observability (log sinks) | axiom (`axiom.infra.log_sinks`) | All extensions | `runtime/config/logging.toml` |
| IAM (auth + authz) | axiom (`axiom.infra.iam`) | All services | OAuth 2.0/OIDC + OpenFGA; single-user mode requires no config |
| Data platform (Iceberg + dbt + Dagster) | axiom (`axiom.data`) | Domain extensions | Capability manifest declares data platform requirements |
| Streaming (Kafka) | Infrastructure (Helm) | axiom data platform, domain extensions | Kafka wire protocol; disabled at Minimal/Small sizes |

### Infrastructure-as-Code Layering

All infrastructure is defined as code using two layers:

- **Terraform** provisions the platform: Kubernetes cluster (K3D locally, EKS /
  GKE / AKS in cloud), managed databases, object storage, networking, IAM.
  Reusable modules live in `axiom/infra/terraform/modules/`. Domain extension
  layers import these modules and add domain-specific resources.

- **Helm** deploys workloads into the cluster Terraform created: application
  pods, services, configmaps, secrets. The unified Helm chart works identically
  on K3D and cloud-managed Kubernetes.

Terraform outputs (e.g., RDS endpoint) feed Helm values (e.g.,
`externalDatabase.host`), bridging the two layers automatically.

### Database Isolation Rules

1. **One PostgreSQL server, separate databases.** Axiom and any domain extension
   layer MUST NOT share a database. Each has its own Alembic migration chain and
   connection URL.

2. **No cross-database foreign keys.** Extensions communicate via events and APIs,
   never via shared tables or direct SQL joins.

3. **Each extension owns its tables.** For example, the `eve_agent` extension owns
   `signals`, `media`, `participants`, `people`. A domain layer's audit extension
   would own its own tables (e.g., `routing_events`, `classification_events`).
   No other extension may write to another extension's tables.

4. **pgvector is axiom's concern.** Only axiom extensions use vector embeddings.
   Domain extensions that need vector search should depend on axiom's RAG
   infrastructure rather than creating parallel pgvector schemas.

### LLM Runtime and Gateway Contract

1. The LLM gateway (`axiom.infra.gateway`) is the **sole entry point** for all LLM
   calls from any extension in any layer.

2. Provider configuration lives in `llm-providers.toml` (runtime config, gitignored).
   The gateway resolves provider selection, handles VPN-gated routing, and enforces
   access control tiers.

3. **The LLM runtime is an axiom-managed dependency.** The axiom installer
   (`axi infra`) agentically discovers the operating environment (GPU availability,
   VRAM, network topology, existing LLM endpoints) and provisions the appropriate
   runtime (Ollama, llama-server, or cloud API). Operators may bring their own
   infrastructure via `localEndpoint` override, but the default path is fully managed.

4. See `prd-managed-infrastructure.md` and `spec-managed-infrastructure.md` for the full lifecycle:
   discovery, provisioning, model management, health monitoring, and upgrade.

### Container Image Strategy

1. **Base image** (`axiom-base:py3.12`): python:3.12-slim + system deps + locked
   Python deps. Rebuilt only when `pyproject.toml` changes.

2. **App images** (signal, api): FROM base image, COPY source, `pip install -e .`.
   Rebuilt on every code change (~seconds with cached base).

3. **CI images**: Use the same base image for test/build jobs to eliminate
   dependency installation time in pipelines.

4. **LLM image**: Separate lifecycle entirely. Ollama/llama-server images are
   pulled from upstream, not built by our CI.

### Network-Aware Discovery and Managed/Unmanaged Boundary

Services may live on the local machine, elsewhere on a private network, or in
the cloud. The installer probes localhost first, then network endpoints
(interactive or via `runtime/config/infra.toml`), then cloud APIs.

Services declared `managed = false` in `infra.toml` are validated but never
modified by axiom. Axiom reports gaps with remediation instructions addressed
to the external administrator. Services declared `managed = true` (or
provisioned by axiom) may be upgraded, restarted, and reconfigured.

See `prd-managed-infrastructure.md` §"Network-Aware Discovery" and
`spec-managed-infrastructure.md` §"Managed vs Unmanaged Services" for details.

### Capability Manifest and Deployment Sizing

Extensions declare their infrastructure requirements via `[extension.requires]`
in their manifest. The framework merges requirements from all extensions and
resolves them against the available environment. Deployment sizing (Minimal
through Enterprise) adapts dynamically based on available hardware and declared
requirements — not static T-shirt sizes.

See `prd-managed-infrastructure.md` §"Deployment Sizing" for reference profiles
and per-service resource allocation tables.

### Extension Contract for Future Axiom Extensions

Any new axiom extension that needs a database MUST:
1. Declare its own SQLAlchemy `Base` and models in its extension directory
2. Maintain its own Alembic migration chain under `extensions/builtins/{ext}/migrations/`
3. Use `AXIOM_DB_URL` (or a dedicated URL if isolation is needed)
4. Never import models from other extensions — use the event bus or API calls

Any new axiom extension that needs an LLM MUST:
1. Use `axiom.infra.gateway` — never call LLM APIs directly
2. Declare its provider requirements in its `axiom-extension.toml`
3. Respect access control tiers from the gateway's routing decisions

## Consequences

- Extensions can be developed, tested, and deployed independently
- Database migrations never conflict between axiom and domain extension layers
- LLM provider changes (new model, endpoint swap) require zero extension code changes
- Infrastructure team can upgrade PostgreSQL or swap LLM runtime without coordinating with extension developers
- The base image strategy eliminates ~30-60s of dependency installation per CI job and per container build
- IAM scales from zero-config single-user to federated multi-facility without application code changes
- The data platform (Iceberg + dbt + Dagster) is the first real consumer of the capability manifest pattern
- Unmanaged services (IT-provisioned, bare-metal) are first-class citizens with validated contracts
- Deployment sizing adapts to hardware without static profiles

## Related Documents

- `prd-managed-infrastructure.md` — Full PRD for all managed services, agent-managed install, sizing
- `spec-managed-infrastructure.md` — Technical spec for discovery, provisioning, validation
- `spec-cicd-and-deployment.md` — CI/CD pipeline and deployment architecture
- `prd-data-platform.md` — Data platform (first consumer of managed infrastructure)
- `spec-data-architecture.md` — Medallion lakehouse technical design
- `prd-agents.md` — Agent design, RACI framework, TRIAGE install role
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
