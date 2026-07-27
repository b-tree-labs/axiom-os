# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Deployment-backend providers for `axi db`.

The `db` extension dispatches `up`/`down`/`delete`/`status` to a
`DeploymentProvider` selected by `[db.deployment] backend` in the
extension manifest. Backends are pluggable — see `base.py` for the
Protocol and `DB_PROVIDERS` registry.

Default backend is `k3d` for back-compat.
"""

from axiom.extensions.builtins.db.providers.base import (
    DB_PROVIDERS,
    DeploymentProvider,
    DeploymentStatus,
    load_deployment_provider,
)
from axiom.extensions.builtins.db.providers.docker_compose import DockerComposeProvider
from axiom.extensions.builtins.db.providers.hosted import HostedProvider
from axiom.extensions.builtins.db.providers.k3d import K3DProvider

__all__ = [
    "DB_PROVIDERS",
    "DeploymentProvider",
    "DeploymentStatus",
    "DockerComposeProvider",
    "HostedProvider",
    "K3DProvider",
    "load_deployment_provider",
]
