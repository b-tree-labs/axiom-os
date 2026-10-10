# axiom-node

One definition of a node's roles. Kubernetes installs it directly; a single
Docker host runs Compose generated from it. See
`docs/specs/spec-cicd-and-deployment.md`, "Node Roles: One Definition for Every
Target", for roles, profiles, priority, secrets, proof and footprint.

```bash
# Kubernetes (K3D, k3s, a cluster)
helm install node infra/charts/axiom-node -f infra/charts/axiom-node/values-lowpower.yaml -f my-values.yaml

# A single host with Docker
python -m axiom.infra.deploy.compose_from_chart --chart infra/charts/axiom-node \
  -f infra/charts/axiom-node/values-lowpower.yaml -f my-values.yaml -o compose.yaml
docker compose up -d
```

Do not edit a generated `compose.yaml`; change the values and generate again.

## Blue/green service names

A role with `blueGreen` runs as slots, so on Compose its services are named
`<release>-<role>-<slot>` (for example `node-edge-blue`, with its init and
companions as `node-edge-blue-init` and `node-edge-blue-conform`), behind a
front service named `<release>-<role>` that owns the published port. An
operator override (`compose.override.yaml`) written against a single-instance
name such as `node-edge-conform` no longer matches a service; Compose then
refuses to start with "neither an image nor a build context". Name the slot.

Literal `$` in a command or an environment value is written as `$$`, because
Compose interpolates and Kubernetes does not.
