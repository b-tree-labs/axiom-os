# ADR-001 (observability): Observability substrate as an Axiom extension

**Status:** Accepted
**Date:** 2026-06-02 (revised 2026-07-13 on rebase: gateway-wiring scope
dropped — the client seam is owned by `axiom.infra.tracing` on main)

> Extension-local ADR per ADR-031 (self-containment). Numbered in this
> extension's own sequence, like every other builtin's `docs/decisions/`.

## Context

Axiom's agents and serving surfaces already produce a rich stream of
trace/eval material. The Python **client** for shipping that stream
lives in `axiom.infra.tracing/` and is complete:

- `TraceProvider` Protocol with `start_trace` / `log_generation` /
  `log_retrieval` / `score` / `flush`.
- `LangfuseTraceProvider` — raw-HTTP implementation (no upstream SDK
  dep; works under Python 3.14).
- `build_trace_provider_from_env(...)` — env-driven backend selection
  (`AXIOM_TRACE_BACKEND` + `LANGFUSE_*`).
- `NullTraceProvider` + `InMemoryTraceProvider` for tests.

Consumers (`axiom.serve.chat_completions`, the evals harness, research
loops) resolve a provider from env and log through it.

What was missing is the **server side**: no install/lifecycle for a
Langfuse instance. Operators had to hand-roll their own helm charts or
point at a third-party cloud. That gap prevented us from claiming
"Axiom is observability-native" when shipping to design partners.

## Decision

Land a new AEOS extension `observability/` that packages the **server
install** of the Langfuse trace+eval substrate alongside the
data-platform:

- Helm chart bundling `langfuse-web` + `langfuse-worker` Deployments,
  Postgres (Langfuse metadata) and ClickHouse (v3 trace store) as
  StatefulSets, plus an Ingress stub. InitContainers wait on Postgres
  + ClickHouse so the web pod never CrashLoopBackoff on first boot —
  no manual `kubectl wait` or post-install patches (the lesson from
  data_platform PR #441).
- Terraform module at `deploy/terraform/` wrapping the same chart via
  `helm_release`, per the platform rule that K8s Server/Platform-tier
  extensions ship **both** Terraform and Helm. Fully generic: every
  value is a variable, secrets are minted via `random_password` when
  not supplied and land in a `kubernetes_secret`.
- Three skills: `observe.install`, `observe.verify`, `observe.diagnose`
  per ADR-056 (CLI verbs are thin skill-fn wrappers).

**Consume, don't re-wire.** This extension does NOT touch the client
seam. `axi observe install` surfaces the resolved `LANGFUSE_HOST` /
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` triple; the operator
binds those env vars on the Axiom process and the existing
`build_trace_provider_from_env()` path auto-selects the `langfuse`
backend. Call-site instrumentation is owned by `axiom.infra.tracing`
and its consumers, not by this extension. (An earlier draft of this
branch also carried gateway-level call wiring; that scope was dropped
on rebase in favor of main's canonical tracing module.)

**Substrate vs policy.** Langfuse is the substrate (where traces
land). Axiom keeps policy (what to evaluate, what thresholds, what to
do on regression). The seam is `TraceProvider`.

**Planned siblings, deferred.** Prometheus + Grafana for system
telemetry are planned as siblings in the same chart, gated by
`--set prometheus.enabled=true` / `--set grafana.enabled=true`. The
values file already carries the keys to make the seam visible. Charts
themselves are not implemented in this PR.

## Consequences

### Positive

- One operator-facing surface (`axi observe`) for the lifecycle of LLM
  observability. The install skill returns the resolved
  `LANGFUSE_HOST`/`PUBLIC_KEY`/`SECRET_KEY`; the operator binds them
  on the Axiom process and the existing env-driven trace provider
  auto-selects Langfuse — no code change required.
- Two equivalent IaC paths to the same helm release: operator-driven
  (`axi observe install`) and pure-Terraform (`deploy/terraform/`).
- Self-resilient chart: no manual patches post-install.

### Negative / open

- **Postgres tenancy honors ADR-052; ClickHouse + Redis do not.**
  Earlier draft of this ADR called the whole stack a carve-out from
  ADR-052. Corrected: Langfuse's Postgres is the shared axiom OLTP
  DB with `?schema=langfuse` on the DATABASE_URL (Prisma honors it),
  a one-time `CREATE SCHEMA IF NOT EXISTS langfuse` + `CREATE
  EXTENSION IF NOT EXISTS pgcrypto` at install. ClickHouse stays as
  its own deployment — it's a different engine entirely (column
  store for high-cardinality traces, not a tenancy decision).
  Redis (Langfuse v3's event queue) likewise stays separate. The
  install skill takes a `pg_dsn` parameter pointing at the shared
  axiom Postgres; only ClickHouse + Redis are bundled by this chart
  by default. Operators wanting full isolation flip `postgres.mode
  = internal` and the chart will bring up a private Postgres too,
  but that's the override, not the default.
- **EC posture deferred.** Whether to run a second Langfuse instance
  for export-controlled traces, or to disable Langfuse on the EC path
  entirely, is not decided here. Follow-up ADR required before any EC
  rollout.
- **Bootstrap project keys.** The chart's Langfuse v3 bootstrap is
  expected to mint the initial project's public/secret keys on first
  boot; the install skill today returns them empty when the caller
  doesn't pre-supply them. Lifecycle TODO: read them back out of a
  Langfuse-side Secret after the web pod becomes ready.

## Related

- ADR-031 — Extension self-containment (this extension owns its own
  deploy artifacts + docs + tests).
- ADR-052 — DB tenancy (the Postgres leg honors it; see above).
- ADR-056 — CLI verbs as thin skill-fn wrappers.
- `axiom.infra.tracing.{provider,langfuse_provider,env,factory}` — the
  client side of the seam (owned by core, unchanged by this extension).
