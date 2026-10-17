# ADR-170: A release is proven in an idle slot before traffic moves, and the whole mechanism is infrastructure as code

**Status:** Proposed (2026-10-06)
**Related:** [ADR-169](adr-169-a-live-surface-is-checked-by-a-scripted-user-journey.md) (synthetic journeys, the gate),
[spec-release-slots.md](../specs/spec-release-slots.md), [spec-cicd-and-deployment.md](../specs/spec-cicd-and-deployment.md)
(describes Terraform and Helm everywhere; this ADR is what makes that true for the serving tier)

## Context

A user-visible failure on a single production node showed how a release reaches users, read from the scripts as
they run and from the node itself:

- A release is `pip install --upgrade` into the one Python environment the running services import from, with
  payload files copied in place and the services restarted after. Rollback is reinstalling the previous tag.
  A bad release is serving the moment it installs. Nothing runs a user-path check between installed and serving.
- The serving tier (gate, application shim, chat UI, front-door proxy) runs as systemd units and plain
  containers. Only the database, a model server, an authorization store and an embeddings service run in the
  node's Kubernetes cluster. The proxy is recreated to change its configuration, which drops connections, and the
  deploy runner cannot do that (its unit blocks privilege escalation), so the step is manual.
- The deployment spec states that every environment runs Kubernetes with the same Helm charts and that `axi
  infra` runs Terraform then Helm. **The code does neither.** The only real chart deploys the secrets store;
  Terraform exists for that chart and for an unused cloud-database module; no workflow runs `helm lint`,
  `helm template`, `terraform validate` or a cluster smoke test; the cluster is created with its ingress
  controller disabled; images are `latest` with pull-never and imported by hand, so two versions cannot run side
  by side.

A first draft of this ADR solved the problem with a hand-built slot layout and a root-owned path unit on the
host. That is a bespoke mechanism outside the code the project reviews, and it needs a person with root. The
owner's direction: blue/green belongs in the infrastructure as code (Terraform, Kubernetes, Helm).

## Decision

1. **Serving workloads are Helm charts.** The gate and application shim are one chart (`axiom-serve`) with a
   `slot` value (`blue` or `green`), an image reference pinned by tag and digest, and the standard hygiene the
   existing manifests lack: readiness and liveness probes, resource limits, a disruption budget.
2. **Two Helm releases exist side by side,** `serve-blue` and `serve-green`, in one namespace. Exactly one is
   active. A release installs or upgrades the **idle** release only.
3. **One stable front door selects the active slot.** A Service named `serve-active` selects pods by
   `slot: <active>`; an ingress routes the public listeners to it. Switching changes the selector, not a process:
   no container is recreated and no connection is dropped. A second Service and host, `serve-preview`, always
   points at the idle slot. On-node clients (telemetry ingest) call `serve-active` through the cluster's published
   port, not a slot's own port.
4. **Terraform owns the switch.** A module declares the cluster, namespace, ingress, and both `helm_release`
   resources, with `active_slot` as an input. The switch is a reviewed change to that value and an apply;
   rollback is the previous value. State lives in a remote backend with locking.
5. **The gate is part of the release, in code.** A Helm test hook runs the site's synthetic journeys
   (ADR-169) as a Job against `serve-preview`; the pipeline applies the switch only if the exit status is `pass`
   (`fail` and `unproven` both refuse), then runs the journeys against the live front door and applies the
   previous `active_slot` automatically on failure, keeping the failed release for diagnosis.
6. **Migrations are a pre-upgrade hook and expand-only.** Both releases run against one database during a
   switch, so a migration must work with the previous release's code; contracting changes ship in a later release.
7. **Shared infrastructure stays outside the slots,** declared once: the database, the model server on the host
   GPU (reached through an ExternalName Service), the authorization store, and the chat UI (the one deployment
   everyone uses).
8. **No privileged host step.** Everything is applied with cluster credentials held by the pipeline identity,
   scoped to the namespace. The IaC is validated in CI like code (`helm lint`, `helm template` into
   `kubeconform`, `terraform fmt` and `validate`, and an ephemeral cluster smoke test).
9. **Images are built and published by the release pipeline,** addressed by digest. `latest` and pull-never are
   removed from the serving path.
10. **The same code runs locally, as a micro deployment.** The chart and the Terraform module have one set of
    code and several profiles (values overlays): `micro` (a laptop in local-host mode), `node` (a single
    production node), and later `cluster`. `axi infra` becomes the one entry point that applies them, replacing
    the inline manifests it applies today. Local use is therefore a rehearsal of production: the same chart, the
    same preview-then-switch release, the same journeys. CI runs its cluster smoke test on the `micro` profile, so
    what CI proves is what a developer runs.

## Options considered

- **A hand-built slot layout with a root-owned reload unit.** Works on one host, but it is bespoke, outside
  review, and requires root. It was the first draft; rejected at the owner's direction.
- **Keep in-place install and add the canary as a post-deploy check.** Detects a bad release after users have it
  and leaves rollback as a second deploy. Kept as the minimum (ADR-169), not the answer.
- **Argo Rollouts or a service mesh for traffic shifting.** More capable (gradual weights) and more to operate on
  one node. Deferred: a Service selector flip is enough for blue/green, and Rollouts can replace the flip later
  without changing the chart.
- **A separate local-development stack that approximates production.** Cheap to start and it drifts: the local
  path today applies inline manifests that production never uses, so local success proves little. Rejected in
  favour of one chart with profiles.
- **Container-only blue/green of the proxy.** Moves traffic but not the thing that changes with a release.
  Rejected as the unit of release.

## Consequences

- A bad release cannot reach users: it fails the gate in the idle release and the active one is untouched.
  Rollback is a one-line change that is rehearsed on every release, and the history of what was live is the
  Terraform state and the repository.
- It is a program, not a patch. The serving tier must move into the cluster, the ingress must be re-enabled and
  given forward-authentication to the gate, and image publishing must exist. Phase 0 (validate the IaC in CI)
  has value on its own and is first. Until the later phases land, the in-place path remains and the canary runs
  as a post-deploy check.
- Costs: a second set of serving pods (memory is ample on the target node; disk headroom must be measured),
  a remote Terraform backend, and discipline on migrations.
- The deployment spec's claims become true instead of aspirational, one phase at a time.
- One definition of the serving tier serves both ends: a developer's laptop and the production node differ in
  values, not in kind. The existing local paths that do not use a cluster (docker-compose with the database only,
  and a native fallback) remain for machines without one, documented as single-slot with no blue/green.
