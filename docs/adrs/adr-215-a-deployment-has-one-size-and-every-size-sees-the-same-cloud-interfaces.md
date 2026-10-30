# ADR-215: A deployment has one size, and every size sees the same cloud interfaces

**Status:** Draft (2026-10-28), for review. Supersedes [ADR-019](adr-019-node-profiles.md) once accepted.
**Related:** [ADR-013](adr-013-k3d-containerd.md) (K3D and containerd), [ADR-015](adr-015-shared-service-boundaries.md) (shared services; object storage through `AXIOM_S3_ENDPOINT`), ADR-170 (blue/green as infrastructure as code), ADR-174 (Postgres at runtime), ADR-182 (never down from our own causes), ADR-189 (a time-series table is declared once), [spec-cicd-and-deployment](../specs/spec-cicd-and-deployment.md) ("Node Roles: One Definition for Every Target"), [spec-managed-infrastructure](../specs/spec-managed-infrastructure.md)

## Context

There are three names for how big a node is, and each one answers a different
question:

| Vocabulary | Values | Where | Answers |
|---|---|---|---|
| ADR-019 node profiles | Edge, Workstation, Server, Platform | `src/axiom/infra/profiles/`, `setup/probe.py` | What is installed |
| Chart profiles | `micro`, `lowpower`, `node`, `cluster` | `infra/charts/axiom-node/values*.yaml` | The resource limits of every role |
| Deployment mode | K3D, k3s, a cluster, Compose generated from the chart | spec-cicd-and-deployment | What runs the containers |

An operator has to learn all three and how they combine, and nothing stops a
combination that makes no sense, such as a Platform profile at `lowpower`
limits on K3D.

At the same time, the first datacenter-scale deployment is being planned on an
HPC center's VM service, not on a commercial cloud. That service has no
managed Kubernetes, no managed database and no IAM, and its VMs are provisioned
by request rather than by API. If the infrastructure code is written against
what that center offers, it will not port to AWS, GCP or Azure, and the next
deployment would be a rewrite.

## Decision

**A deployment has exactly one size: `large`, `medium`, `small` or `micro`.**
The size fixes the runtime and the default resource limits. What is installed
is a set of role switches in values, not part of the size. The chart is the
only source: Kubernetes installs it with Helm, and a host without Kubernetes
runs Compose rendered from it.

| Size | Runtime | Typical host | Replaces |
|---|---|---|---|
| `large` | Multi-node, CNCF-conformant Kubernetes: a managed cluster (EKS, GKE, AKS) or RKE2 on three or more VMs | A datacenter platform; an institution-scale deployment | Platform; chart `cluster` |
| `medium` | k3s, single server | A rack server; a staging environment; a heavier site node | Server; chart `node` at server limits |
| `small` | K3D | A laptop or desktop, for development and rehearsal | Edge, Workstation; chart `node` |
| `micro` | No Kubernetes: Compose rendered from the chart | A collector host with about 2 GB of memory; later, mobile and embedded devices | chart `lowpower` and `micro` |

**Every size sees the same interfaces as a commercial cloud.** The chart
depends only on standard Kubernetes and protocol-level contracts, never on a
provider's API. Where a provider offers a managed service that meets the
contract, the deployment binds to it. Where it does not, as on an HPC center's
VMs or on a laptop, a self-hosted component provides the same contract under
the same name.

| Contract the chart uses | AWS | GCP | Azure | Self-hosted (large on VMs, medium, small) |
|---|---|---|---|---|
| Kubernetes API, conformant | EKS | GKE | AKS | RKE2 (`large`), k3s (`medium`), K3D (`small`) |
| `Service type: LoadBalancer` | NLB via the AWS Load Balancer Controller | Cloud Load Balancing | Azure Load Balancer | kube-vip or MetalLB (`large`); k3s ServiceLB |
| Ingress class `axiom` (Gateway API when it is ready) | ingress-nginx behind the NLB | the same | the same | ingress-nginx |
| TLS certificates | cert-manager with Let's Encrypt or ACME | the same | the same | the same |
| DNS records | external-dns to Route 53 | external-dns to Cloud DNS | external-dns to Azure DNS | external-dns where the DNS provider allows it; otherwise a record made once by hand |
| Storage classes `axiom-fast` (databases) and `axiom-standard` | EBS gp3 | Persistent Disk SSD | Premium SSD | OpenEBS LocalPV on the VM's local disk (`large`); local-path (`medium`, `small`) |
| Object storage over the S3 API, through `AXIOM_S3_ENDPOINT` | S3 | Cloud Storage through its S3-compatible XML API and HMAC keys | No native S3 API: SeaweedFS in the cluster, tiering to Blob | A provider S3 endpoint if the host offers one, or SeaweedFS |
| Postgres with TimescaleDB and pgvector | CloudNativePG in the cluster | the same | the same | the same |
| Secrets, as `ClusterSecretStore` `axiom` | External Secrets Operator to Secrets Manager | to Secret Manager | to Key Vault | to the node vault, or OpenBao |
| Metrics and logs | kube-prometheus-stack and Loki | the same | the same | the same |
| GPUs | NVIDIA GPU Operator | the same | the same | the same |
| Deploys | Flux in the cluster, pulling a release tag | the same | the same | the same |
| Images | `ghcr.io` | the same | the same | the same |

**Postgres runs in the cluster on every cloud, not as RDS, Cloud SQL or Azure
Database.** TimescaleDB is not offered on RDS or Cloud SQL, and Azure offers
only the Apache-licensed edition, which has no compression. The storage plan
(ADR-189: hypertables with compression after a fixed window) depends on
compression. CloudNativePG gives the same operator, failover and
backup-to-object-storage everywhere, so the database is identical from `large`
to `medium`. A managed database remains an option for a deployment that does
not need time-series compression; it binds through the same DSN secret.

**The infrastructure code has four layers, and each layer hands the next the
same outputs everywhere.**

| Layer | What it is | Outputs |
|---|---|---|
| 0. Hosts | Terraform: a cloud module (VPC, node groups), the OpenStack provider for an OpenStack cloud, or a static inventory module for VMs provisioned by request | Host addresses, disk layout, network ranges |
| 1. Cluster | The managed cluster, or RKE2, k3s or K3D bootstrapped on the hosts | A kubeconfig |
| 2. Cluster base | A new chart, `axiom-cluster`: ingress, cert-manager, external-dns, storage classes, CloudNativePG, SeaweedFS, External Secrets, monitoring, the GPU operator and Flux. Each component has a `provided` switch: when the platform already supplies it, the chart installs nothing and only binds the name. | The contract names in the table above |
| 3. Application | `axiom-node`, unchanged in shape: roles, blue/green slots and jobs | A running node |

Layers 2 and 3 are the same Helm charts at every size from `small` to
`large`. Only layer 0 and layer 1 know which provider they are on. `micro`
renders layer 3 as Compose and has no layers 1 and 2. Its database, object
store and secrets are the containers and files the Compose rendering already
declares.

**The chart's `profile` key takes the size.** `micro`, `small`, `medium` and
`large` are its values. The old values `lowpower`, `node` and `cluster` remain
accepted aliases for one minor release, with a warning (expand, then contract,
per ADR-182), and are then removed.

## Options considered

- **Keep three vocabularies (ADR-019 plus chart profiles plus mode).** This lost
  because nothing prevents nonsensical combinations, and every guide has to
  explain all three.
- **Two axes: a size for the runtime and limits, and ADR-019's names for what
  is installed.** This lost because what is installed is already expressed as
  role switches in values. A second set of names adds learning, not control.
- **Write the datacenter deployment against the HPC center's own services**
  (its VM API, its storage mounts, its accounts). This lost because it would
  not port. The center's offerings become implementations of the contracts,
  behind the same names a cloud uses.
- **Managed Postgres on clouds, in-cluster Postgres elsewhere.** This lost
  because the managed services lack TimescaleDB compression, and two database
  paths would double the upgrade and backup testing.
- **Kubernetes on `micro` too.** This lost on measurement. The control plane
  costs about 700 MiB that a 2 GB collector host does not have (about 135 MiB
  for the roles on Compose against about 830 MiB on K3D, spec-cicd-and-deployment).
- **k3s for `large` instead of RKE2.** This is viable, since k3s supports
  several servers. RKE2 is preferred because it has the same lineage and
  tooling as k3s, with CIS hardening and FIPS options that a datacenter's
  security review is likely to ask about. A deployment may choose k3s; the
  contract does not change.

## Consequences

- One word tells an operator how a deployment runs. `axi infra` and the
  installer probe recommend a size, not a profile.
- Moving a deployment between providers changes layers 0 and 1 and a values
  file. Layers 2 and 3 do not change.
- A self-hosted `large` deployment carries work a cloud would do: operating the
  CloudNativePG cluster, SeaweedFS when there is no provider S3, OS patching and
  the cluster upgrade. That work is the price of portability, and it is the same
  work `medium` already does.
- There is no IAM on self-hosted platforms, so workloads get scoped static
  credentials from the secret store. On clouds, workload identity (IRSA, GKE
  Workload Identity, Azure Workload Identity) can replace them later behind the
  same `ClusterSecretStore`.

**Follow-up work:**

1. Rename the chart's profiles and add the aliases (`values-micro.yaml`,
   `values-small.yaml`, `values-medium.yaml`, `values-large.yaml`), with
   `helm lint` and `kubeconform` across all four.
2. Build the `axiom-cluster` chart with its `provided` switches, and a
   conformance check that a cluster exposes every contract name in the table.
3. Add an object-store role (SeaweedFS) and route bronze and backups through
   `axiom.infra.storage.s3.S3StorageProvider`.
4. Write Terraform layer 0 modules: the static inventory first (for VMs
   provisioned by request), then OpenStack and one commercial cloud.
5. Replace `src/axiom/infra/profiles/` and the probe's recommendation with
   sizes, and update spec-managed-infrastructure and spec-cicd-and-deployment.
6. Run the zero-gap upgrade smoke on `medium` and `large`, as it already runs on
   Compose and K3D.
