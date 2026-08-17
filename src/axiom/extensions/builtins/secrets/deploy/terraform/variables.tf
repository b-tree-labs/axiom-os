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
  description = "Namespace for the secrets substrate."
  type        = string
  default     = "axiom-secrets"
}

variable "release" {
  description = "Helm release name."
  type        = string
  default     = "axiom-secrets"
}

variable "chart_path" {
  description = "Path to the bundled helm chart. Empty = the chart shipped next to this module."
  type        = string
  default     = ""
}

variable "values_file" {
  description = "Path to a values overlay (values-local / values-selfhosted / values-enclave). Empty = chart defaults (sealed)."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Image — split so an air-gapped install points `image_registry` at a
# local mirror without touching repository/tag.
# ---------------------------------------------------------------------------

variable "image_registry" {
  description = "Image registry host. Point at a local mirror for air-gapped installs."
  type        = string
  default     = "docker.io"
}

variable "image_repository" {
  description = "OpenBao image repository."
  type        = string
  default     = "openbao/openbao"
}

variable "image_tag" {
  description = "OpenBao image tag (pin to a vetted digest for air-gapped installs)."
  type        = string
  default     = "2.2.0"
}

# ---------------------------------------------------------------------------
# Server posture
# ---------------------------------------------------------------------------

variable "server_mode" {
  description = "dev = in-memory auto-unsealed ephemeral (LOCAL ONLY); sealed = persistent, sealed, operator/KMS-unsealed."
  type        = string
  default     = "sealed"

  validation {
    condition     = contains(["dev", "sealed"], var.server_mode)
    error_message = "server_mode must be \"dev\" or \"sealed\"."
  }
}

variable "storage_size" {
  description = "PVC size for the OpenBao data volume (sealed mode)."
  type        = string
  default     = "2Gi"
}

variable "service_type" {
  description = "Kubernetes Service type for the client-facing service (ClusterIP | NodePort | LoadBalancer)."
  type        = string
  default     = "ClusterIP"
}

variable "mount" {
  description = "kv/v2 mount path the extension addresses — surfaced as AXIOM_OPENBAO_MOUNT."
  type        = string
  default     = "kv"
}

# ---------------------------------------------------------------------------
# dev-mode root token. Sealed mode ignores this (no token Secret is
# rendered) — the operator mints a scoped token via `bao token create`
# after unseal. Supply here ONLY for server_mode = dev.
# ---------------------------------------------------------------------------

variable "dev_root_token" {
  description = "Dev-mode root token (server_mode = dev only). Ignored in sealed mode."
  type        = string
  default     = ""
  sensitive   = true
}

# ---------------------------------------------------------------------------
# Arbitrary chart-value passthrough
# ---------------------------------------------------------------------------

variable "extra_values" {
  description = "Additional chart values as a flat map of --set style key/values."
  type        = map(string)
  default     = {}
}
