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

output "langfuse_host_hint" {
  description = "In-cluster LANGFUSE_HOST for the env-driven trace provider (axiom.infra.tracing.env)."
  value       = "http://${var.release}-web.${var.namespace}.svc:3000"
}

output "credentials_secret" {
  description = "Secret holding the (possibly minted) credentials — read minted values back from here."
  value       = kubernetes_secret_v1.credentials.metadata[0].name
}
