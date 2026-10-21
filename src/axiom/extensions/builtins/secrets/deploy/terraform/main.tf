# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Pure-IaC leg of the secrets substrate: namespace + the Helm release
# wrapping the OpenBao chart at ../helm. Equivalent to installing the
# chart with `helm install`; pick whichever fits the deploy story.
# Replaying this module with different variables (image_registry,
# values_file, server_mode) is the intended migration path between hosts
# — a self-hosted install and an air-gapped enclave differ only by the
# overlay + registry passed here.
#
# The module deliberately mints NO secrets: OpenBao is custody, and its
# unseal keys / root token are produced by `bao operator init` inside the
# running server and escrowed OUTSIDE this cluster (see deploy/README.md).
# Terraform never sees, stores, or state-persists an unseal key.

terraform {
  required_version = ">= 1.5"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.30"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 3.0"
    }
  }
}

provider "kubernetes" {
  config_path = var.kubeconfig_path
}

provider "helm" {
  kubernetes = {
    config_path = var.kubeconfig_path
  }
}

locals {
  chart_path = var.chart_path != "" ? var.chart_path : "${path.module}/../helm"
}

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

# ---------------------------------------------------------------------------
# The Helm release — same chart `helm install` drives. A values overlay
# (values-local / values-selfhosted / values-enclave) supplies the profile;
# the `set` list below is the small delta an operator commonly overrides.
# ---------------------------------------------------------------------------

resource "helm_release" "this" {
  name      = var.release
  namespace = kubernetes_namespace_v1.this.metadata[0].name
  chart     = local.chart_path

  # Profile overlay (empty = chart defaults: sealed, docker.io image).
  values = var.values_file != "" ? [file(var.values_file)] : []

  set = concat(
    [
      {
        name  = "image.registry"
        value = var.image_registry
      },
      {
        name  = "image.repository"
        value = var.image_repository
      },
      {
        name  = "image.tag"
        value = var.image_tag
      },
      {
        name  = "server.mode"
        value = var.server_mode
      },
      {
        name  = "server.storage.size"
        value = var.storage_size
      },
      {
        name  = "service.type"
        value = var.service_type
      },
      {
        name  = "extension.mount"
        value = var.mount
      },
    ],
    [for k, v in var.extra_values : { name = k, value = v }],
  )

  # dev-mode root token only. NEVER set in sealed mode (the chart renders
  # no token Secret there). A literal list — Terraform lifts leaf
  # sensitivity onto the whole list under concat/conditionals, at which
  # point the provider can't read the required `name`. Empty value is
  # harmless in sealed mode; supply via this variable (not the overlay
  # file) when server_mode = dev so it isn't clobbered.
  set_sensitive = [
    {
      name  = "server.dev.rootToken"
      value = var.dev_root_token
    },
  ]

  depends_on = [kubernetes_namespace_v1.this]
}
