# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

# ---------------------------------------------------------------------------
# Cluster / chart targeting
# ---------------------------------------------------------------------------

variable "kubeconfig_path" {
  description = "Path to the kubeconfig for the target cluster."
  type        = string
}

variable "namespace" {
  description = "Namespace for the observability substrate."
  type        = string
  default     = "axiom-observability"
}

variable "release" {
  description = "Helm release name."
  type        = string
  default     = "axiom-observability"
}

variable "chart_path" {
  description = "Path to the bundled helm chart. Empty = the chart shipped next to this module."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Service exposure
# ---------------------------------------------------------------------------

variable "service_type" {
  description = "Kubernetes Service type for langfuse-web (ClusterIP | NodePort | LoadBalancer)."
  type        = string
  default     = "ClusterIP"
}

variable "node_port" {
  description = "NodePort for langfuse-web when service_type = NodePort. 0 = let the cluster pick."
  type        = number
  default     = 0
}

# ---------------------------------------------------------------------------
# Postgres tenancy — external (shared OLTP, schema=langfuse; the default
# per extension ADR-001 / platform ADR-052) vs internal (private
# StatefulSet from the chart).
# ---------------------------------------------------------------------------

variable "postgres_mode" {
  description = "external = ride the shared OLTP Postgres (schema=langfuse); internal = chart brings up its own StatefulSet."
  type        = string
  default     = "external"

  validation {
    condition     = contains(["external", "internal"], var.postgres_mode)
    error_message = "postgres_mode must be \"external\" or \"internal\"."
  }
}

variable "pg_dsn" {
  description = "Shared-Postgres DSN for postgres_mode = external (schema=langfuse is appended by the chart). Required in external mode."
  type        = string
  default     = ""
  sensitive   = true
}

# ---------------------------------------------------------------------------
# Secrets — empty string means "mint one" (random_password); read the
# minted values back from the <release>-credentials Secret afterwards.
# ---------------------------------------------------------------------------

variable "salt" {
  description = "Langfuse SALT. Empty = generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "nextauth_secret" {
  description = "Langfuse NEXTAUTH_SECRET. Empty = generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "encryption_key" {
  description = "Langfuse ENCRYPTION_KEY. Empty = generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "postgres_password" {
  description = "Password for the chart-internal Postgres (postgres_mode = internal only). Empty = generate."
  type        = string
  default     = ""
  sensitive   = true
}

variable "clickhouse_password" {
  description = "Password for the bundled ClickHouse. Empty = generate."
  type        = string
  default     = ""
  sensitive   = true
}

# ---------------------------------------------------------------------------
# Optional static local storage — pin PVs to a directory on a specific
# node (no dynamic provisioner). Off unless BOTH node_name and
# data_path are set; the chart then binds its PVCs via the module's
# StorageClass. Generic: any node, any path — never a root filesystem.
# ---------------------------------------------------------------------------

variable "node_name" {
  description = "Kubernetes node name the local PVs are pinned to (kubernetes.io/hostname). Empty = no static PVs; rely on the cluster's default StorageClass."
  type        = string
  default     = ""
}

variable "data_path" {
  description = "Directory on the node's data volume backing the PVs (subdirs clickhouse/ and postgres/ must exist). Empty = no static PVs."
  type        = string
  default     = ""
}

variable "clickhouse_storage" {
  description = "ClickHouse PV/PVC size."
  type        = string
  default     = "50Gi"
}

variable "postgres_storage" {
  description = "Internal-Postgres PV/PVC size (postgres_mode = internal only)."
  type        = string
  default     = "20Gi"
}

# ---------------------------------------------------------------------------
# Arbitrary chart-value passthrough
# ---------------------------------------------------------------------------

variable "extra_values" {
  description = "Additional chart values as a flat map of --set style key/values."
  type        = map(string)
  default     = {}
}
