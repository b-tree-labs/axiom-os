# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Deployment-backend Protocol + registry for `axi db`.

A `DeploymentProvider` is a backend that knows how to bring a
PostgreSQL service up/down/delete and report its status. Concrete
backends live alongside this module: K3DProvider, DockerComposeProvider,
HostedProvider.

Configuration in v1:

  - Backend selection: `AXIOM_DB_BACKEND` env var
    (default: "k3d" for back-compat).
  - Per-backend config: defaults baked into the provider
    classes (e.g., DockerComposeProvider's compose file).

Manifest-based per-environment config (`[extension.deployment]`)
is deferred — see ADR-001 for the rationale (AEOS 0.1 schema
doesn't yet permit a custom per-extension config namespace).
The config loader (`db.config.load_deployment_config`) already
supports an `[extension.deployment]` block for ephemeral manifests
used in tests and for forward-compatibility once AEOS evolves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DeploymentStatus:
    """Backend-agnostic status report.

    `extra` holds backend-specific keys (e.g., K3D cluster name,
    Docker Compose service state). Callers display the well-known
    fields and may surface `extra` when verbose.
    """

    backend: str  # "k3d" | "docker-compose" | "hosted"
    available: bool  # backend tooling installed + reachable
    running: bool  # service is actually up
    connection_url: str | None = None  # masked or unmasked? caller decides
    extra: dict[str, Any] = field(default_factory=dict)


class DeploymentProvider(Protocol):
    """Backend for `axi db up/down/delete/status`.

    All methods are synchronous and return a boolean for success
    (except `status()` which returns structured info). Methods may
    print progress to stdout — the CLI handler prints framing
    (banner, "Next steps"); the provider prints backend-specific
    detail.

    Implementations MUST:
    - Be idempotent where reasonable (calling `up()` twice should
      be safe if already up).
    - Print actionable error messages when prerequisites are missing
      (e.g., "Docker is not running. Start Docker Desktop and retry").
    - Return False on failure; never raise — the CLI relies on the
      exit-code convention.
    """

    name: str

    def up(self) -> bool:
        """Start the database backend. Idempotent."""
        ...

    def down(self) -> bool:
        """Stop the database backend (preserves data)."""
        ...

    def delete(self) -> bool:
        """Delete the database and all data. Destructive."""
        ...

    def status(self) -> DeploymentStatus:
        """Report backend + service status."""
        ...


# Registry of available backends. Populated at module import time by
# providers/__init__.py importing each concrete provider. Keep this
# dict the source of truth for "what backends does `axi db` know about"
# — `axi db --help` and config validation both reference it.
DB_PROVIDERS: dict[str, type] = {}


def register_provider(name: str, cls: type) -> None:
    """Register a provider class under its config-key name.

    Idempotent: re-registering the same name with the same class is
    a no-op; re-registering with a different class raises.
    """
    existing = DB_PROVIDERS.get(name)
    if existing is None:
        DB_PROVIDERS[name] = cls
        return
    if existing is cls:
        return
    raise ValueError(
        f"Provider name conflict: {name!r} is already registered as "
        f"{existing.__name__}; cannot also register {cls.__name__}"
    )


def load_deployment_provider(
    backend: str | None = None,
    backend_config: dict[str, Any] | None = None,
) -> DeploymentProvider:
    """Construct the configured DeploymentProvider.

    Args:
        backend: backend name (e.g., "k3d"). If None, falls through
            to env-var override or back-compat default ("k3d").
        backend_config: per-backend kwargs. If None, uses each
            provider's built-in defaults.

    Raises:
        ValueError: backend name is not registered.
    """
    if backend is None or backend_config is None:
        from axiom.extensions.builtins.db.config import load_deployment_config

        config = load_deployment_config()
        backend = backend or config.backend
        backend_config = backend_config if backend_config is not None else config.backend_kwargs(backend)

    if backend not in DB_PROVIDERS:
        known = ", ".join(sorted(DB_PROVIDERS)) or "(none registered)"
        raise ValueError(
            f"Unknown db deployment backend: {backend!r}. "
            f"Known backends: {known}"
        )

    cls = DB_PROVIDERS[backend]
    return cls(**backend_config)
