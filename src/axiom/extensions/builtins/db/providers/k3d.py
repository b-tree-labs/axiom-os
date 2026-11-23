# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""K3D deployment provider for `axi db`.

Wraps the existing K3D lifecycle helpers in signals/pgvector_store
(`k3d_up`, `k3d_down`, `k3d_delete`, `k3d_status`) behind the
DeploymentProvider Protocol. This is the original (pre-INFRA-2)
behavior of `axi db up` — preserved here for back-compat as the
default backend.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.db.providers.base import (
    DeploymentStatus,
    register_provider,
)


class K3DProvider:
    """Local PostgreSQL + pgvector via K3D (Kubernetes-in-Docker).

    Requires `k3d` CLI installed (`brew install k3d`) and Docker
    running. Use `DockerComposeProvider` for a simpler, k3d-free
    alternative.
    """

    name = "k3d"

    def __init__(self, cluster_name: str | None = None, **_: Any) -> None:
        # cluster_name reserved for future use; today K3D_CLUSTER_NAME
        # is a module constant in pgvector_store. Accept the kwarg now
        # so the manifest's [db.deployment.k3d] block can pass it.
        self.cluster_name = cluster_name

    def up(self) -> bool:
        from axiom.extensions.builtins.signals.pgvector_store import k3d_up

        return bool(k3d_up())

    def down(self) -> bool:
        from axiom.extensions.builtins.signals.pgvector_store import k3d_down

        return bool(k3d_down())

    def delete(self) -> bool:
        from axiom.extensions.builtins.signals.pgvector_store import k3d_delete

        return bool(k3d_delete())

    def status(self) -> DeploymentStatus:
        from axiom.extensions.builtins.signals.pgvector_store import (
            K3D_CLUSTER_NAME,
            k3d_status,
        )

        raw = k3d_status() or {}
        # k3d_installed absent → assume installed (status() succeeded);
        # only k3d_installed == False explicitly marks the tooling missing.
        available = raw.get("k3d_installed", True) is not False
        running = bool(raw.get("running", False))

        return DeploymentStatus(
            backend=self.name,
            available=available,
            running=running,
            connection_url=(
                "postgresql://axiom:axiom@localhost:5432/axiom_db"
                if running
                else None
            ),
            extra={
                "cluster_name": K3D_CLUSTER_NAME,
                "exists": bool(raw.get("exists", False)),
                "servers": raw.get("servers", 0),
                "agents": raw.get("agents", 0),
                "k3d_installed": raw.get("k3d_installed", True),
            },
        )


register_provider("k3d", K3DProvider)
