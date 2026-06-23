# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.providers`` — Protocol + factory registry + built-ins.

Two-layer split, mirroring the LLM / log-sink / storage / source-kind
provider patterns elsewhere in the codebase:

    SecretStoreProvider   — factory; constructed once from a config dict;
                             advertises capabilities; produces SecretStore
                             instances via ``open()``.
    SecretStore           — runtime client; per-request get/put/delete;
                             optional lease/rotate when the provider advertises.
    SecretStoreRegistry   — kind → provider class map; ``create(kind, cfg)``.

Concrete providers register themselves at import time. SEC-2 ships
``openbao`` + ``env``; SEC-3 will add ``kubernetes`` (CSI).
"""

from .protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)
from .registry import SecretStoreRegistry

# Concrete providers — imported AFTER the protocol + registry so their
# import-time registration in ``_register`` works without circularity.
from .env import EnvSecretStoreProvider  # noqa: E402
from .gcp_secret_manager import GCPSecretManagerProvider  # noqa: E402
from .kubernetes import KubernetesSecretStoreProvider  # noqa: E402
from .openbao import OpenBaoSecretStoreProvider  # noqa: E402
from ._register import register_builtins  # noqa: E402

register_builtins()


__all__ = (
    "Capabilities",
    "EnvSecretStoreProvider",
    "GCPSecretManagerProvider",
    "KubernetesSecretStoreProvider",
    "OpenBaoSecretStoreProvider",
    "Secret",
    "SecretRef",
    "SecretStore",
    "SecretStoreProvider",
    "SecretStoreRegistry",
    "register_builtins",
)
