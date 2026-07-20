# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Default provider registration. Imported at extension load to populate
the SecretStoreRegistry with the built-in providers."""

from __future__ import annotations

from .aws_secrets_manager import AWSSecretsManagerProvider
from .azure_key_vault import AzureKeyVaultProvider
from .env import EnvSecretStoreProvider
from .gcp_secret_manager import GCPSecretManagerProvider
from .kubernetes import KubernetesSecretStoreProvider
from .openbao import OpenBaoSecretStoreProvider
from .registry import SecretStoreRegistry


def register_builtins() -> None:
    """Idempotent registration of the built-in providers."""
    SecretStoreRegistry.register(EnvSecretStoreProvider)
    SecretStoreRegistry.register(OpenBaoSecretStoreProvider)
    SecretStoreRegistry.register(KubernetesSecretStoreProvider)
    SecretStoreRegistry.register(GCPSecretManagerProvider)
    SecretStoreRegistry.register(AWSSecretsManagerProvider)
    SecretStoreRegistry.register(AzureKeyVaultProvider)


__all__ = ["register_builtins"]
