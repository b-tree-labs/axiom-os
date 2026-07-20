# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Pure-IaC leg of the observability substrate: namespace + credentials
# Secret + (optional) static local PVs + the Helm release wrapping the
# chart at ../helm. Equivalent to `axi observe install`; pick whichever
# fits the deploy story. Replaying this module with different variables
# is the intended migration path between hosts.

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
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
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

  # Static local-PV pinning activates only when BOTH knobs are set.
  pin_local_storage = var.node_name != "" && var.data_path != ""
  internal_postgres = var.postgres_mode == "internal"

  salt                = var.salt != "" ? var.salt : random_password.salt[0].result
  nextauth_secret     = var.nextauth_secret != "" ? var.nextauth_secret : random_password.nextauth_secret[0].result
  encryption_key      = var.encryption_key != "" ? var.encryption_key : random_password.encryption_key[0].result
  clickhouse_password = var.clickhouse_password != "" ? var.clickhouse_password : random_password.clickhouse[0].result
  postgres_password   = var.postgres_password != "" ? var.postgres_password : random_password.postgres[0].result
}

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

# ---------------------------------------------------------------------------
# Secret minting — parity with `axi observe install` (empty = generate).
# ---------------------------------------------------------------------------

resource "random_password" "salt" {
  count   = var.salt == "" ? 1 : 0
  length  = 43
  special = false
}

resource "random_password" "nextauth_secret" {
  count   = var.nextauth_secret == "" ? 1 : 0
  length  = 43
  special = false
}

resource "random_password" "encryption_key" {
  count   = var.encryption_key == "" ? 1 : 0
  length  = 43
  special = false
}

resource "random_password" "clickhouse" {
  count   = var.clickhouse_password == "" ? 1 : 0
  length  = 28
  special = false
}

resource "random_password" "postgres" {
  count   = var.postgres_password == "" ? 1 : 0
  length  = 28
  special = false
}

# Credentials escrow: minted (or supplied) secrets, readable by the
# operator after apply. The chart renders its own workload Secrets from
# the same values — this one exists so nothing minted is ever lost.
resource "kubernetes_secret_v1" "credentials" {
  metadata {
    name      = "${var.release}-credentials"
    namespace = kubernetes_namespace_v1.this.metadata[0].name
  }
  data = merge(
    {
      salt                = local.salt
      nextauth_secret     = local.nextauth_secret
      encryption_key      = local.encryption_key
      clickhouse_password = local.clickhouse_password
    },
    local.internal_postgres ? { postgres_password = local.postgres_password } : {},
  )
}

# ---------------------------------------------------------------------------
# Optional static local storage: no dynamic provisioner — PVs pinned to
# a directory on one node's data volume. Skipped entirely unless the
# operator sets node_name + data_path.
# ---------------------------------------------------------------------------

resource "kubernetes_storage_class_v1" "local" {
  count = local.pin_local_storage ? 1 : 0
  metadata {
    name = "${var.release}-local"
  }
  storage_provisioner = "kubernetes.io/no-provisioner"
  volume_binding_mode = "WaitForFirstConsumer"
  reclaim_policy      = "Retain"
}

resource "kubernetes_persistent_volume_v1" "clickhouse_data" {
  count = local.pin_local_storage ? 1 : 0
  metadata {
    name = "${var.release}-clickhouse-data"
  }
  spec {
    capacity = {
      storage = var.clickhouse_storage
    }
    access_modes                     = ["ReadWriteOnce"]
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = kubernetes_storage_class_v1.local[0].metadata[0].name
    persistent_volume_source {
      local {
        path = "${var.data_path}/clickhouse"
      }
    }
    node_affinity {
      required {
        node_selector_term {
          match_expressions {
            key      = "kubernetes.io/hostname"
            operator = "In"
            values   = [var.node_name]
          }
        }
      }
    }
  }
}

resource "kubernetes_persistent_volume_v1" "postgres_data" {
  count = local.pin_local_storage && local.internal_postgres ? 1 : 0
  metadata {
    name = "${var.release}-postgres-data"
  }
  spec {
    capacity = {
      storage = var.postgres_storage
    }
    access_modes                     = ["ReadWriteOnce"]
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = kubernetes_storage_class_v1.local[0].metadata[0].name
    persistent_volume_source {
      local {
        path = "${var.data_path}/postgres"
      }
    }
    node_affinity {
      required {
        node_selector_term {
          match_expressions {
            key      = "kubernetes.io/hostname"
            operator = "In"
            values   = [var.node_name]
          }
        }
      }
    }
  }
}

# ---------------------------------------------------------------------------
# The Helm release — same chart `axi observe install` drives.
# ---------------------------------------------------------------------------

resource "helm_release" "this" {
  name      = var.release
  namespace = kubernetes_namespace_v1.this.metadata[0].name
  chart     = local.chart_path

  set = concat(
    [
      {
        name  = "postgres.mode"
        value = var.postgres_mode
      },
      {
        name  = "service.type"
        value = var.service_type
      },
      {
        name  = "clickhouse.internal.storage"
        value = var.clickhouse_storage
      },
      {
        name  = "postgres.internal.storage"
        value = var.postgres_storage
      },
    ],
    var.node_port != 0 ? [
      {
        name  = "service.nodePort"
        value = tostring(var.node_port)
      },
    ] : [],
    local.pin_local_storage ? [
      {
        name  = "clickhouse.internal.storageClass"
        value = kubernetes_storage_class_v1.local[0].metadata[0].name
      },
      {
        name  = "postgres.internal.storageClass"
        value = kubernetes_storage_class_v1.local[0].metadata[0].name
      },
    ] : [],
    [for k, v in var.extra_values : { name = k, value = v }],
  )

  # NOTE: a literal list, not concat()/conditionals — functions lift the
  # leaf sensitivity of these values onto the whole list, at which point
  # the provider cannot see the (required) `name` attributes. Both
  # postgres keys are always set; the chart only reads the one matching
  # postgres.mode.
  set_sensitive = [
    {
      name  = "langfuse.salt"
      value = local.salt
    },
    {
      name  = "langfuse.nextauthSecret"
      value = local.nextauth_secret
    },
    {
      name  = "langfuse.encryptionKey"
      value = local.encryption_key
    },
    {
      name  = "clickhouse.internal.password"
      value = local.clickhouse_password
    },
    {
      name  = "postgres.internal.password"
      value = local.postgres_password
    },
    {
      name  = "postgres.external.dsn"
      value = var.pg_dsn
    },
  ]

  depends_on = [
    kubernetes_secret_v1.credentials,
    kubernetes_persistent_volume_v1.clickhouse_data,
    kubernetes_persistent_volume_v1.postgres_data,
  ]
}
