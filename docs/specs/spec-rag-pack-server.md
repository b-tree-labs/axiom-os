# RAG Pack Server & Generation Pipeline

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-20
**Last Updated:** 2026-04-02
**Layer:** Axiom core
**Related:** `spec-rag-architecture.md`, `spec-rag-community.md`, `spec-rag-knowledge-maturity.md`

> **Implementation Status (2026-04-02):**
> - ✅ `.axiompack` format (create, verify, extract, install, remove) — 21 tests
> - ✅ Pack server client (list, download, publish, health_check) — tested
> - ✅ EC safety guard (export_controlled blocked locally, allowed in PrivateCloud) — tested
> - ✅ Supply chain verification (verify_pack_integrity, SHA256 checksum validation) — 11 tests
> - 🔲 SeaweedFS deployment on Private-Server (Phase A) — not yet
> - 🔲 SeaweedFS deployment on PrivateCloud (Phase B) — not yet
> - 🔲 `pack_install` step type in installer.py (Phase C) — not yet
> - 🔲 Community CDN integration (Phase D) — not yet

---

## Terms Used

| Term | Definition | Reference |
|------|-----------|-----------|
| `.axiompack` | Versioned, gzip-tar artifact: `manifest.json` + `chunks.parquet` + `SHA256SUMS` | `spec-rag-architecture.md §3c` |
| `pack_id` | Stable, lowercase hyphen-separated identifier for a pack (e.g., `netl-triga`) | `axiom glossary pack_id` |
| `domain_tag` | Free-form tag linking chunks to a facility domain (e.g., `netl-triga`) | `axiom glossary domain_tag` |
| `access_tier` | Sensitivity axis: `public · restricted · export_controlled` | `spec-rag-architecture.md §2` |
| `pack server` | SeaweedFS instance (or compatible S3 endpoint) hosting `.axiompack` files and a registry | `axiom glossary pack_server` |
| `rag-org` | The live DuckDB corpus from which packs are generated | `spec-rag-architecture.md §3a` |
| EC pack | A `.axiompack` with `access_tier = "export_controlled"` — never installable locally | `axiom glossary ec_pack` |

All terms resolve via `axiom glossary <term>` or `docs/glossary-axiom.toml`.

---

## 1. Problem Statement

Axiom operates across three distinct trust boundaries:

- **Local workstation** — developer or researcher laptop; no institutional controls on the filesystem
- **Private-Server** — facility-managed k3d cluster; VPN-gated, trusted but not export-controlled
- **PrivateCloud allocation** — HPC environment with EAR / 10 CFR 810 controls; some data cannot leave

Pre-built RAG context packs need to flow across these environments. Manually copying `.axiompack` files is error-prone, does not enforce access-tier controls, and does not compose with automated deployment (`axiom install`). The current spec-rag-architecture.md covers the wire format and local DuckDB store; it does not cover where packs are hosted, how they are generated from a live corpus, or how export-controlled packs are prevented from landing on uncontrolled filesystems.

This spec owns the server-side hosting infrastructure and the generation pipeline.

---

## 2. Pack Server Architecture

### 2.1 Deployment Profiles

Three deployment profiles are defined. Each resolves to a SeaweedFS (or compatible) endpoint registered as a `pack-server` connection.

**Profile A — Private-Server (facility, restricted)**

SeaweedFS in k3d, port 9000, VPN-gated. Bucket: `axiom-packs`. Packs have `access_tier = "restricted"`. Credentials supplied via `PACK_SERVER_KEY` environment variable. Helm values addition to `values-private-server.yaml` shown in §8.

**Profile B — PrivateCloud (export-controlled)**

Identical SeaweedFS deployment inside the PrivateCloud allocation. Never reachable from outside the allocation boundary. Packs in this instance carry `access_tier = "export_controlled"`. The `axiom rag pack install` command refuses to download EC packs to a local DuckDB — it exits non-zero with a legal constraint message (§7). EC packs are query-time remote only: they are retrieved at query time via the fan-out client (see `spec-rag-architecture.md §3b`) and never persisted outside the PrivateCloud boundary.

**Profile C — Community CDN (future)**

S3 or Cloudflare R2, public, governed by `spec-rag-community.md §7`. Out of scope for Phase A/B implementation. Domain packs produced from `community_facts` are distributed via this profile. This spec documents the interface but does not specify CDN configuration.

### 2.2 Architecture Diagram

```mermaid
graph TB
    subgraph Client["Client (any env)"]
        CLI["axiom rag pack — install / query"]
        DDB["Local DuckDB — restricted packs only"]
        CLI --> DDB
    end

    subgraph Private-Server["Private-Server k3d (VPN)"]
        RS_SEAWEEDFS["SeaweedFS :9000 — bucket: axiom-packs"]
        RS_REG["registry.json — access_tier: restricted"]
        RS_SEAWEEDFS --> RS_REG
    end

    subgraph PrivateCloud["PrivateCloud Allocation"]
        TC_SEAWEEDFS["SeaweedFS — bucket: axiom-packs"]
        TC_REG["registry.json — access_tier: export_controlled"]
        TC_SEAWEEDFS --> TC_REG
    end

    subgraph CDN["Community CDN (future)"]
        S3["S3 / R2 public"]
        CDN_REG["registry.json — access_tier: public"]
        S3 --> CDN_REG
    end

    CLI -- "download (restricted)" --> RS_SEAWEEDFS
    CLI -- "remote query only — no local install" --> TC_SEAWEEDFS
    CLI -- "download (public)" --> S3

    style Client fill:#e3f2fd,color:#000000
    style Private-Server fill:#c8e6c9,color:#000000
    style PrivateCloud fill:#fff3e0,color:#000000
    style CDN fill:#f3e5f5,color:#000000

    style CLI      fill:#1565c0,color:#ffffff
    style DDB      fill:#1565c0,color:#ffffff
    style RS_SEAWEEDFS fill:#2e7d32,color:#ffffff
    style RS_REG   fill:#2e7d32,color:#ffffff
    style TC_SEAWEEDFS fill:#e65100,color:#ffffff
    style TC_REG   fill:#e65100,color:#ffffff
    style S3       fill:#6a1b9a,color:#ffffff
    style CDN_REG  fill:#6a1b9a,color:#ffffff

    linkStyle 0 stroke:#1565c0,stroke-width:2px
    linkStyle 1 stroke:#2e7d32,stroke-width:2px
    linkStyle 2 stroke:#e65100,stroke-width:2px
    linkStyle 3 stroke:#6a1b9a,stroke-width:2px
    linkStyle 4 stroke:#1565c0,stroke-width:2.5px
    linkStyle 5 stroke:#e65100,stroke-width:2.5px,stroke-dasharray:5 5
    linkStyle 6 stroke:#6a1b9a,stroke-width:2.5px
```

---

## 3. Pack Registry API

Each pack server exposes a lightweight registry at a well-known path. The registry is regenerated on each `axiom rag pack publish` call.

### 3.1 List Registry

```
GET /packs/registry.json
```

No authentication required for listing (SeaweedFS anonymous read on the registry object). Pack download requires credentials.

Response schema:

```json
{
  "server_id": "private-server-facility",
  "access_tier": "restricted",
  "generated_at": "2026-03-20T10:00:00Z",
  "packs": [
    {
      "pack_id": "netl-triga",
      "domain_tag": "netl-triga",
      "latest_version": "1.0.0",
      "versions": ["1.0.0"],
      "access_tier": "restricted",
      "fact_count": 847,
      "size_bytes": 124000000,
      "description": "PartnerLab facility system procedures, safety analysis, operational history",
      "download_url": "https://private-server.example.org:9000/axiom-packs/netl-triga-v1.0.0.axiompack"
    }
  ]
}
```

`server_id` is set once at SeaweedFS configuration time and persisted in the registry object. `access_tier` at the server level is the minimum tier for all packs hosted; individual packs may be more restrictive but never less.

### 3.2 Download Pack

```
GET /packs/{pack_id}/{version}.axiompack
```

Authenticated download. SeaweedFS presigned URL (time-limited, 1 hour) or direct download with `Authorization: Bearer {PACK_SERVER_KEY}` header, depending on SeaweedFS policy configuration.

### 3.3 Convenience Redirect

```
GET /packs/{pack_id}/latest.axiompack
```

Server-side redirect (HTTP 302) to the latest version object. Implemented as a SeaweedFS lifecycle rule or a thin nginx/Traefik rewrite in the k3d ingress.

---

## 4. Generation Pipeline

### 4.1 `axiom rag pack generate`

```
axiom rag pack generate --domain <domain_tag> [--corpus rag-org] [--version <semver>] [--output <dir>]
axiom rag pack generate --all [--corpus rag-org] [--output <dir>]
```

`--all` enumerates distinct `domain_tags` in the target corpus and runs one generation pass per tag. Run on a host with direct access to the live `rag-org` DuckDB corpus.

Pipeline steps executed in order:

1. **Query chunks** — `SELECT * FROM rag_chunks WHERE corpus = 'rag-org' AND domain_tags @> ARRAY[$domain_tag]`. If `--domain` is omitted, all chunks in the corpus are included.
2. **Export Parquet** — write `chunks.parquet` using `duckdb COPY ... TO ... (FORMAT PARQUET)`. Columns: `chunk_id`, `text`, `embedding`, `domain_tags`, `access_tier`, `source_uri`, `knowledge_maturity`, `created_at`.
3. **Write manifest** — `manifest.json` with fields: `pack_id`, `domain_tag`, `version`, `corpus`, `embedding_model`, `embedding_dim`, `fact_count`, `generated_at`, `generated_by` (hostname), `axiom_version`.
4. **Compute checksums** — SHA-256 of `manifest.json` and `chunks.parquet`, written to `SHA256SUMS`.
5. **Archive** — `tar -czf {domain_tag}-v{version}.axiompack manifest.json chunks.parquet SHA256SUMS`.
6. **Print summary** — path, size in MB, fact_count, embedding model.

Wire format is defined by `spec-rag-architecture.md §3c`; this spec owns only the generation tooling that produces conformant files.

### 4.2 `axiom rag pack publish`

```
axiom rag pack publish <file.axiompack> [--server <server-name>]
```

Steps:

1. Read `manifest.json` from archive to extract `pack_id`, `version`, `access_tier`.
2. Verify SHA256SUMS before uploading.
3. Upload `.axiompack` to `{pack_server_url}/axiom-packs/{pack_id}-v{version}.axiompack` using SeaweedFS client (`mc`) or `boto3`.
4. Download current `registry.json` (or create empty), merge new pack entry (upsert by `pack_id` + `version`), re-upload.
5. Print: uploaded URL, updated registry pack count.

`--server` selects a registered `pack-server` connection by name. Defaults to the connection named `pack-server` if exactly one is registered; errors if ambiguous.

---

## 5. Connection Definition

A new connection type is registered in the RAG extension manifest:

**File:** `src/axiom/extensions/builtins/rag/axiom-extension.toml`

```toml
[[connections]]
name = "pack-server"
display_name = "RAG Pack Server"
kind = "api"
endpoint = ""
credential_env_var = "PACK_SERVER_KEY"
credential_type = "api_key"
health_check = "http_get"
health_endpoint = ""         # resolved at setup time to {endpoint}/packs/registry.json
category = "storage"
capabilities = ["read", "write"]
required = false
post_setup_module = "axiom.extensions.builtins.rag.connections"
post_setup_function = "setup_pack_server"
```

`setup_pack_server()` behavior:

1. Prompt for endpoint URL (e.g., `https://private-server.example.org:9000`) and API key.
2. Write to settings: `rag.pack_server_url` and `rag.pack_server_key`.
3. Perform health check GET to `{url}/packs/registry.json`.
4. On success, print the server's `server_id`, `access_tier`, and a table of available packs.
5. On failure, print error and instructions to check VPN connectivity.

Multiple `pack-server` connections may be registered with distinct names (e.g., `pack-server-private-server`, `pack-server-private`). The `--server` flag on `generate`, `publish`, `list`, and `install` commands selects by name.

---

## 6. install.toml Integration

### 6.1 New Step Type: `pack_install`

Add `pack_install` to the step type registry in `installer.py`:

```toml
[[environments.steps]]
id          = "pack-install-netl-triga"
type        = "pack_install"
description = "Install PartnerLab facility facility knowledge pack"
pack_id     = "netl-triga"
server      = "pack-server"
depends_on  = "connect-pack-server"
```

The installer executes `axiom rag pack install {pack_id} --server {server}` as the step action. The step is skipped if the pack is already installed at the latest version (idempotent).

### 6.2 Private-Server Environment Steps

```toml
[[environments.steps]]
id          = "connect-pack-server"
type        = "connect"
description = "Connect to Private-Server RAG pack server"
connection  = "pack-server"
depends_on  = "rag-db-init"

[[environments.steps]]
id          = "pack-install-netl-triga"
type        = "pack_install"
description = "Install PartnerLab facility facility knowledge pack"
pack_id     = "netl-triga"
server      = "pack-server"
depends_on  = "connect-pack-server"
```

### 6.3 PrivateCloud Environment Steps

```toml
[[environments.steps]]
id          = "connect-pack-server-private"
type        = "connect"
description = "Connect to PrivateCloud export-controlled pack server"
connection  = "pack-server-private"
depends_on  = "rag-db-init"

# Note: no pack_install step for EC packs.
# EC packs are query-time remote only. See spec-rag-pack-server.md §7.
```

---

## 7. EC Pack Safety Guard

This is a hard enforcement rule, not a documentation note.

When `axiom rag pack install` resolves a pack (from a remote registry or a local file), it reads `manifest.json` and checks `access_tier` before writing anything to the local DuckDB.

If `access_tier == "export_controlled"`:

1. **Print error:**
   ```
   ERROR: EC packs cannot be installed locally.
   Export-controlled content is subject to EAR and 10 CFR 810 restrictions.
   Use `axiom rag query --remote --server <server>` to query this pack on the
   authorized server without transferring controlled content to this machine.
   ```
2. **Exit non-zero** (exit code 3, reserved for access control violations).
3. **Log the attempt** — write a structured record to the audit log (`runtime/logs/audit.jsonl`) with fields: `event = "ec_pack_install_blocked"`, `pack_id`, `server`, `timestamp`, `user` (from system user), `hostname`.

The guard is implemented in `axiom.extensions.builtins.rag.cli` in the `pack_install` command handler, before any DuckDB write. It cannot be bypassed by flags. Any future `--force` flag must be gated on a `NEUT_OVERRIDE_EC_GUARD=1` environment variable that itself requires explicit documentation of the legal justification.

---

## 8. SeaweedFS Helm Values

Addition to `infra/helm/values-private-server.yaml`:

```yaml
seaweedfs:
  enabled: true
  mode: standalone
  persistence:
    size: 50Gi
  buckets:
    - name: axiom-packs
      policy: none
      purge: false
  resources:
    requests:
      memory: 512Mi
      cpu: 250m
```

`policy: none` disables anonymous download. Registry JSON (`registry.json`) is granted anonymous read via a separate SeaweedFS bucket policy applied at setup time, so `axiom rag pack list --remote` works without credentials.

For PrivateCloud, an identical block is added to `values-privatecloud.yaml` with `persistence.size: 200Gi` to accommodate larger export-controlled corpora.

---

## 9. CLI Reference

```bash
# Generation — run on a host with direct rag-org corpus access
axiom rag pack generate --domain <tag>           # generate pack for one domain_tag
axiom rag pack generate --all                    # generate one pack per domain_tag in rag-org
axiom rag pack generate --domain <tag> \
  --version 1.2.0 --output ./dist              # explicit version + output dir
axiom rag pack publish <file.axiompack>           # upload to default pack-server
axiom rag pack publish <file.axiompack> \
  --server pack-server-private                    # upload to named server

# Distribution — run on any client
axiom rag pack list --remote                     # list packs on default server
axiom rag pack list --remote --server <name>     # list packs on named server
axiom rag pack install <pack-id>                 # download latest + install to local DuckDB
axiom rag pack install <pack-id> --version 1.0.0 # install specific version
axiom rag pack install <pack-id> \
  --server pack-server-private-server                  # install from named server
axiom rag pack install <file.axiompack>           # install from local file (offline)
axiom rag pack update                            # update all installed packs to latest
axiom rag pack update <pack-id>                  # update one pack
axiom rag pack pin <pack-id> <version>           # lock pack to specific version
axiom rag pack status                            # list installed packs, versions, staleness
axiom rag pack remove <pack-id>                  # uninstall pack from local DuckDB
```

---

## 10. Implementation Phases

| Phase | Deliverables | Blocks |
|-------|-------------|--------|
| A | SeaweedFS on Private-Server k3d; `generate`, `publish`, `list --remote`, `install`; `netl-triga` as first pack | Local + Private-Server Qwen+RAG pipeline |
| B | SeaweedFS on PrivateCloud; EC pack generation; EC safety guard (§7) | PrivateCloud developer-researcher RAG |
| C | `pack_install` step type in `installer.py`; Private-Server + PrivateCloud install.toml steps (§6) | `axiom install` automation |
| D | Community CDN integration; `public` tier packs from `community_facts` | Community corpus distribution |

Phase A is the minimum viable pack distribution path. Phase B requires a PrivateCloud allocation with SeaweedFS storage quota approved. Phase C requires Phase A and B registry endpoints to be stable. Phase D is governed by `spec-rag-community.md §7` and is out of scope for the initial implementation sprints.

---

## 11. Invariants

These rules are enforced in code, not advisory:

- **EC packs never install locally.** The guard in §7 is unconditional. No flag bypasses it without an explicit environment variable override documented with a legal justification.
- **SHA-256 verified before load.** `axiom rag pack install` verifies `SHA256SUMS` against the downloaded archive before opening any DuckDB transaction. A checksum mismatch aborts with exit code 4 and logs the failure.
- **`axiom rag pack install` is idempotent.** If the same `pack_id` + `version` is already present in the local DuckDB, the command prints a no-op message and exits 0 without re-downloading or re-writing.
- **Installing a new version does not remove the old one.** Old versions remain queryable unless `--replace` is passed explicitly. This allows pinned agents to continue operating on a known-good version while a new version is evaluated.
- **`pack_id` + `version` is the unique key.** The registry and local DuckDB both enforce this uniqueness constraint. A publish that duplicates an existing `pack_id` + `version` without `--overwrite` is rejected.
- **Registry regeneration is atomic.** `axiom rag pack publish` uploads the new pack object before updating `registry.json`, so a partial publish leaves the registry in a consistent (if stale) state.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
