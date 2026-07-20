# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Config loader for the `db` extension's deployment block.

V1 selects the backend via the `AXIOM_DB_BACKEND` env var (default
"k3d" for back-compat). Per-backend kwargs come from the provider
classes' built-in defaults.

Reads `[extension.deployment]` (and per-backend subsections) from
any `axiom-extension.toml` passed in directly — used by tests with
ephemeral manifests, and forward-compatible with a future AEOS
schema that allows a per-extension config namespace.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MANIFEST_FILENAME = "axiom-extension.toml"
_DEFAULT_BACKEND = "k3d"  # back-compat with pre-INFRA-2 behavior


@dataclass
class DeploymentConfig:
    """Parsed `[db.deployment]` block."""

    backend: str = _DEFAULT_BACKEND
    per_backend: dict[str, dict[str, Any]] = field(default_factory=dict)

    def backend_kwargs(self, backend: str) -> dict[str, Any]:
        """Return the per-backend kwargs dict (empty if absent)."""
        return dict(self.per_backend.get(backend, {}))


def _find_manifest() -> Path | None:
    """Locate the db extension's axiom-extension.toml.

    Looks alongside the `db` package (the canonical install location).
    """
    try:
        from axiom.extensions.builtins import db as db_pkg
    except ImportError:
        return None
    candidate = Path(db_pkg.__file__).resolve().parent / _MANIFEST_FILENAME
    return candidate if candidate.exists() else None


def load_deployment_config(manifest_path: Path | None = None) -> DeploymentConfig:
    """Load and parse the `[db.deployment]` block.

    Env-var override `AXIOM_DB_BACKEND` wins over the manifest, so
    contributors can flip backends without editing config.
    """
    path = manifest_path or _find_manifest()
    backend = _DEFAULT_BACKEND
    per_backend: dict[str, dict[str, Any]] = {}

    if path is not None and path.exists():
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
        deployment = (doc.get("extension") or {}).get("deployment") or {}
        if "backend" in deployment:
            backend = str(deployment["backend"])
        for key, value in deployment.items():
            if key == "backend":
                continue
            if isinstance(value, dict):
                per_backend[key] = dict(value)

    # Env override last so it always wins.
    env_backend = os.environ.get("AXIOM_DB_BACKEND")
    if env_backend:
        backend = env_backend

    return DeploymentConfig(backend=backend, per_backend=per_backend)
