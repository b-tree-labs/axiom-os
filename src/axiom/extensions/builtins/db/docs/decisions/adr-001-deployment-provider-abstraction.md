# ADR-001 (db extension): Deployment-Provider Abstraction

**Status:** Accepted (2026-05-19)
**Scope:** `axi db` extension only
**Supersedes:** —

## Context

`axi db up`, `down`, `delete`, and `status` were hardcoded to the
**K3D** (Kubernetes-in-Docker) deployment backend by direct calls
into `axiom.extensions.builtins.signals.pgvector_store`'s
`k3d_up`/`k3d_down`/`k3d_delete`/`k3d_status` functions. A
parallel manual deployment path existed via
`axiom/infra/docker-compose.yml` but was not dispatched through
`axi db` — operators had to know which command to use when, and
the docs-compose YAML was effectively orphan tooling.

This violates the **Provider Protocol pattern** the Axiom platform
uses for every other extension axis (LLM providers, signal
sources, storage providers, etc.) and creates the
"two-alternatives, pick one" UX trap: the same operator action
(`bring up the database`) requires picking a different command
depending on which backend you want.

It also blocked contributors who couldn't or wouldn't install K3D
locally — the docker-compose path existed but wasn't first-class.

## Decision

Introduce a `DeploymentProvider` Protocol in
`axiom.extensions.builtins.db.providers` and dispatch every `axi
db` lifecycle command through a registry-resolved provider
selected by `[db.deployment] backend` in
`axiom-extension.toml` (overridable per-environment via
`AXIOM_DB_BACKEND` env var).

Three backends ship in the initial cut:

| Backend | Use case | Requirements |
|---|---|---|
| `k3d` (default; back-compat) | Local dev for contributors with K3D installed | `brew install k3d` |
| `docker-compose` | Local dev with just Docker Desktop | Docker Desktop or equivalent |
| `hosted` | Staging / production / CI against an externally-managed PostgreSQL | Connection string via config or `AXIOM_DB_URL` |

Adding a new backend (`kubernetes`, `nomad`, etc.) is a single
file + manifest entry; no CLI changes.

## Consequences

### Positive

- **One command per operator action.** `axi db up` always
  means "bring up the configured backend"; no per-backend
  remembering.
- **AEOS pattern consistency.** Matches LLM/signal/storage
  provider conventions; new contributors learn the pattern
  once.
- **Contributor-friendliness.** Docker Desktop is more
  universally installed than K3D; flipping
  `[db.deployment] backend = "docker-compose"` removes the
  K3D-install onboarding step.
- **Multi-environment uniformity.** Same CLI works against
  local K3D, local Compose, staging hosted Postgres, prod
  hosted Postgres. Differences are config, not commands.
- **Testability.** Each backend has its own unit-test surface;
  CLI dispatch is integration-tested with mocked providers.

### Negative

- **One more abstraction layer.** Reading the call chain now
  goes through `cmd_up → load_deployment_provider → provider.up()`
  rather than direct `k3d_up()`. Tradeoff is the lifecycle
  consistency.
- **Status output is more generic.** Loses some K3D-specific
  detail (the K3D status block was rich); the new generic
  `DeploymentStatus.extra` dict surfaces the same data but in
  a less customized format. If a backend needs richer
  status, it can render its own block.

### Neutral

- `cmd_migrate` and `cmd_bootstrap` remain on the direct
  signals/pgvector_store path for this iteration. They're
  schema-management concerns, not deployment-backend
  concerns, and refactoring them is out of scope for this
  ADR. A follow-up (INFRA-3 in the working-tickets doc) will
  promote `get_engine()` / `get_session()` to a shared
  utility and bring Alembic migration into the same
  consumer-of-shared-helpers shape.

- **Manifest-based per-environment config is deferred.** The
  AEOS 0.1 manifest schema (`aeos-manifest-0.1.json`) only
  permits a whitelisted set of top-level keys (`agent`,
  `chat_tools`, `connections`, `extension`, `extractors`,
  `mcp_servers`, `prompt_contributions`, `providers`,
  `skills`) and locks down `extension`'s sub-properties to
  the AEOS-defined set. There's no current home for a
  per-extension `[extension.deployment]` or `[db.deployment]`
  block.

  V1 ships with **env-var override only** (`AXIOM_DB_BACKEND`)
  and the per-backend defaults baked into the provider
  classes (compose file path, default Postgres URL, etc.).

  The config loader (`config.py`) still supports reading
  manifest-based config — when AEOS adds a generic
  per-extension config namespace, restoring the block is
  a one-line manifest change with no code rework. A
  follow-up ticket should propose the AEOS spec extension.

## Implementation

- `providers/base.py` — `DeploymentProvider` Protocol +
  `DeploymentStatus` dataclass + `register_provider()` +
  `load_deployment_provider()` registry helpers.
- `providers/k3d.py` — `K3DProvider` wrapping the existing
  signals/pgvector_store helpers (zero behavior change for
  the default backend).
- `providers/docker_compose.py` — `DockerComposeProvider`
  wrapping `docker compose -f <file> up|stop|down`.
- `providers/hosted.py` — `HostedProvider` for externally-
  managed PostgreSQL; lifecycle commands are no-ops with
  a refusal for `delete`.
- `config.py` — manifest loader for `[db.deployment]` block
  with env-var override.
- `tests/test_providers.py` — registry, config-loading,
  per-provider unit tests, and integration tests for
  `load_deployment_provider()` end-to-end.

## Out of scope (separate follow-ups)

- **INFRA-3** — `axiom.infra.db` shared session-factory utility
  (promotes `get_engine()` / `get_session()` from signals to a
  reusable module).
- AEOS runtime infrastructure-dependency declaration
  (`requires_runtime_services`) so extensions can declare
  "I need PostgreSQL" at install/run time and installation
  tooling can react.
- Bridging the one-shot `axi doctor` checks to also become
  extension-contributable (today only TRIAGE's heartbeat
  surface is extensible per
  `project-triage-diagnostic-extension-contract`).
