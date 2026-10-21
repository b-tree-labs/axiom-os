# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

output "release_name" {
  description = "Helm release name."
  value       = helm_release.this.name
}

output "namespace" {
  description = "Namespace the substrate landed in."
  value       = kubernetes_namespace_v1.this.metadata[0].name
}

output "openbao_url" {
  description = "In-cluster OpenBao address — the value of AXIOM_OPENBAO_URL for the openbao SecretStoreProvider."
  value       = "http://${var.release}.${var.namespace}.svc:8200"
}

output "openbao_mount" {
  description = "kv/v2 mount path — the value of AXIOM_OPENBAO_MOUNT."
  value       = var.mount
}

output "dev_token_secret" {
  description = "dev-mode only: name of the Secret holding AXIOM_OPENBAO_TOKEN. Empty/unused in sealed mode (mint a scoped token via `bao token create`)."
  value       = var.server_mode == "dev" ? "${var.release}-token" : ""
}
