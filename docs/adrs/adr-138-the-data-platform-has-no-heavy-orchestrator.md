# ADR-138 — The data platform has no heavy orchestrator

**Status:** Accepted
**Date:** 2026-09-28
**Supersedes:** ADR-049 (data-platform orchestration boundary), in part — its
orchestrator choice only. Its connector contract and its binary
lakehouse-vs-signal rule stand.
**Related:** ADR-128 (medallion tier boundaries), ADR-131 (there is always a
medallion), ADR-049 (superseded in part)

## Context

ADR-049 decided that **Dagster owns the data platform** — the only thing that
materializes lakehouse assets, with PLINTH triggering and monitoring its runs.
That was accepted on 2026-05-28 and never became true.

What the tree actually held on 2026-09-28, sixteen months of releases later:

- `dagster>=1.7` in `pyproject.toml`, carrying the comment **"not-yet-used"**.
- Three files importing it, all of them inside `dagster_app/`, which nothing
  outside `dagster_app/` imported.
- A helm chart that unconditionally rendered a webserver, a daemon, an
  instance ConfigMap and a meta-database init Job.
- On the one deployment that exists, **zero** Dagster jobs, ever.

The pods were not idle, though. They had become load-bearing for things that
have nothing to do with orchestration: five callers exec'd into the webserver
to mint an OAuth token, because that pod happened to hold a credential and
have axiom installed, and a manifest sync exec'd into the daemon to read a
PersistentVolumeClaim. When the locally built image was garbage-collected,
all of it stopped at once — and a self-healing sweep spent weeks restarting a
Deployment whose image no longer existed.

An orchestrator nobody schedules against is not neutral. It is a pod with
credentials in it.

## Decision

**There is no heavy orchestration tier.** Dagster is removed from Axiom: the
dependency, `dagster_app/`, `Dockerfile.dagster`, and the four chart templates
that existed only to run it.

1. **The direct runner is the only runner.** ADR-049 §4 already described it —
   a connector is "consumed by whichever orchestrator the deployment tier
   provides — a Dagster sensor/asset in the heavy tier; a minimal direct
   runner on a lean/offline node with no lakehouse." There is no heavy tier,
   so the second clause is the whole sentence. Conform and ingest run through
   `axi data` verbs and scheduled units.
2. **ADR-049 §3 and §4 stand unchanged.** `IngestSource` is still a portable
   connector contract, not an orchestrator, and data still either has a
   durable table home or is an ephemeral agent signal. Removing the runtime
   does not disturb either.
3. **Sense is untouched.** ADR-049 §2 said Sense is not a data-pipeline
   orchestrator. It still is not, and this does not promote it.
4. **PLINTH keeps everything except the thing it was monitoring.** ADR-049 §6
   split scheduling authority (Dagster's) from agent judgment (PLINTH's).
   Only the first half is retired.

## Consequences

- `axi data install` no longer takes `--expose`, `--node-port` or
  `--axiom-version`. All three set Dagster values and nothing else; a flag
  that changes nothing is worse than a missing one, because it reads as
  configuration.
- `data.diagnose` no longer checks two Deployments that will not exist, and
  `data.troubleshoot` no longer gathers logs from them.
- The chart renders 4 objects instead of 10. Storage — the database, the
  bronze PVC, the connectors ConfigMap — is untouched.
- **Re-adopting an orchestrator is a new decision, not a revert.** If
  scheduled materialization is wanted later, it should be chosen against the
  requirement of the day rather than inherited from a dependency that sat
  unused for sixteen months.
- On an existing deployment the Deployments and the meta database are not
  dropped by this change. `helm upgrade` removes the workloads; the meta
  database is left for an operator to drop deliberately.
