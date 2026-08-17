# ADR-017: Release Pipeline, Dependency Propagation, and Supply Chain Integrity

**Status:** Accepted
**Date:** 2026-04-01
**Authors:** Benjamin Booth, Claude

## Context

Axiom is a framework consumed by domain applications (each with its own repo,
CI, and release cadence). When Axiom publishes a new version, downstream
consumers must learn about it, validate compatibility, and decide whether to
adopt it. Today this is entirely manual: a developer notices a new Axiom tag,
edits `pyproject.toml`, runs tests, and pushes. Nothing prevents a stale
dependency from drifting for weeks.

At the same time, deployed nodes running a domain application need a safe path
to receive updates. A node operator should never be surprised by an update, and
an update should never land without passing the same validation chain that
installation uses (TIDY validate, smoke tests, RACI approval).

Finally, the 2025 axios supply chain attack (malicious publish of a hijacked
npm package affecting thousands of downstream consumers) demonstrated that
even trusted registries are attack surfaces. Axiom publishes wheels and
container images consumed by domain applications and deployed nodes. If a
compromised build were to enter that pipeline, the blast radius includes every
downstream consumer and every node running the platform. We must treat
provenance and integrity as first-class concerns, not afterthoughts.

## Decisions

### 1. Three-Stage Release Pipeline

Release propagation flows through three stages, each with its own gate:

```
Stage 1: Axiom Release
  Tag pushed (v0.X.Y) → CI → tests + lint + build → publish wheel + image
  Gate: all CI checks green, tag is GPG-signed

Stage 2: Consumer Dependency Update
  Axiom release triggers a PR in each consumer repo
  Gate: consumer CI passes (including regression tests against new Axiom)

Stage 3: Node Update with Consent
  Consumer release triggers TIDY update check on deployed nodes
  Gate: operator approval via RACI + TIDY validation
```

No stage auto-promotes to the next without its gate passing.

### 2. Axiom Publishes Signed Artifacts

Every Axiom release produces:

- **Python wheel** published to a package registry (initially GitHub Releases,
  optionally PyPI)
- **Container images** published to ghcr.io (`axiom-signal`, `axiom-api`)
- **SBOM** (Software Bill of Materials) in CycloneDX format, attached to the
  GitHub Release
- **Provenance attestation** via GitHub Actions artifact attestations (SLSA
  Level 2), linking each artifact to the specific workflow run, commit, and
  inputs that produced it
- **Checksums** (SHA-256) for all published artifacts

The SBOM and attestation allow any consumer or node to verify:
- *What* went into the build (SBOM)
- *How* it was built, and that CI produced it (attestation)
- *That it hasn't been tampered with* (checksums)

### 3. Consumer Repos Use Automated Dependency PRs

When Axiom publishes a new release, each consumer repo (e.g., a domain
application) receives an automated PR that:

1. Bumps the Axiom pin in `pyproject.toml` (e.g., `axiom @ git+...@v0.2.1`)
2. Runs the consumer's full CI suite against the new version
3. Includes the Axiom changelog diff in the PR body
4. Is labeled `dependency-update` and assigned to the repo maintainer

**Implementation:** A GitHub Actions workflow in the consumer repo, triggered
by `repository_dispatch` from Axiom's release workflow, or by a scheduled
check (`gh release list`) if cross-repo dispatch is not configured.

**The consumer never auto-merges.** A human reviews the PR, confirms CI is
green, and merges. This is the supply chain boundary: the consumer maintainer
attests that they've reviewed what changed in their dependency.

### 4. Consumers Pin Exact Versions, Not Ranges

Consumer `pyproject.toml` pins Axiom to an exact tag:

```toml
dependencies = [
    "axiom @ git+https://github.com/b-tree-labs/axiom-os.git@v0.2.1",
]
```

Not `>=0.2.0`, not `~=0.2`. Exact pins ensure reproducible builds and prevent
a compromised latest version from silently entering the build. The automated
PR is the only mechanism that changes the pin.

### 5. Lock Files for Transitive Dependencies

Both Axiom and consumer repos generate a lock file (`requirements.lock`) via
`pip-compile` or `uv pip compile` from `pyproject.toml`. The lock file is
committed and CI validates that it matches:

```bash
pip-compile --generate-hashes pyproject.toml -o requirements.lock
pip install --require-hashes -r requirements.lock
```

`--generate-hashes` ensures every transitive dependency is verified by SHA-256
at install time. A supply chain compromise of a transitive dependency
(the axios pattern) would fail hash verification and break the build loudly.

### 6. Node Updates Are TIDY's Responsibility

TIDY already owns infrastructure lifecycle (provision, validate, maintain).
Node updates are an extension of the "maintain" phase:

- TIDY periodically checks for available updates (configurable interval,
  default: daily)
- When a new version is available, TIDY compares changelogs and runs a
  compatibility pre-check
- TIDY follows the operator's RACI configuration for the `platform.upgrade`
  action:

  | RACI Level | TIDY Behavior |
  |-----------|--------------|
  | **Inform** | Log "v0.2.1 available" + emit signal event |
  | **Consult** | Notify operator with changelog, await explicit approval |
  | **Act** | Apply update within maintenance window, validate, auto-rollback on failure |

- The update itself uses the same tooling as installation:
  - K3D: `helm upgrade` with new image tag (rolling, zero-downtime)
  - Bare-metal: `pip install axiom==0.2.1` + service restart
- Post-update, TIDY runs `validate` and the end-to-end smoke test
- If validation fails, TIDY rolls back and escalates to the operator

### 7. No Auto-Update Without Explicit Opt-In

The default RACI level for `platform.upgrade` is **Consult** (notify and wait).
An operator must explicitly set it to **Act** to enable auto-updates. Even in
Act mode, TIDY:
- Never updates during active user sessions (unless the operator overrides)
- Respects maintenance windows if configured
- Always validates post-update and rolls back on failure
- Logs every update action to the audit trail

### 8. Container Image Verification at Pull Time

When TIDY pulls a new container image during an update, it verifies:
1. The image digest matches the one published in the release metadata
2. The provenance attestation chains back to the expected CI workflow
3. The SBOM is present and the dependency list hasn't grown unexpectedly

If any check fails, TIDY refuses the update and escalates.

### 9. Vulnerability Scanning in CI

Both Axiom and consumer CI pipelines include:
- `pip-audit` (or `osv-scanner`) on every PR and release build
- Known-vulnerability check against the SBOM
- Alerts on any dependency with a published CVE
- CI fails on critical/high CVEs; warns on medium/low

This catches the *other* supply chain vector: not a compromised package, but
a package with a known vulnerability that hasn't been updated.

### 10. Separation of Build and Publish Credentials

CI workflows use two distinct credential scopes:
- **Build** jobs have read-only access to the repo and dependencies
- **Publish** jobs have write access to the registry, triggered only by
  tag pushes on the default branch, and require the build job to pass

This limits the blast radius of a compromised CI job: a build job cannot
publish, and a publish job only runs after all gates pass.

## Consequences

**Positive:**
- Every artifact is traceable from node back to source commit
- Supply chain compromises (both direct and transitive) are caught by hash
  verification and attestation checks
- Node operators have full control over when and how updates land
- The dependency update cadence is visible (PRs, not silent installs)

**Negative:**
- Maintaining lock files and SBOMs adds CI complexity
- Exact pins mean more frequent dependency PRs (each Axiom release = 1 PR
  per consumer)
- Operators who want "just keep it updated" must explicitly opt in

**Mitigations:**
- Lock file generation and SBOM creation are automated in CI — no manual work
- Dependency PRs are generated automatically with full context
- The RACI Act level exists for operators who trust the pipeline

## Related Documents

- `prd-managed-infrastructure.md` — TIDY lifecycle, validation chain, update strategies
- `spec-cicd-and-deployment.md` — Build pipeline, container strategy, publishing
- `spec-security.md` — Credential management, audit logging
- `adr-012-provider-identity.md` — Three-layer provider traceability
- `adr-016-multi-node-federation.md` — Node discovery, peer health monitoring
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
