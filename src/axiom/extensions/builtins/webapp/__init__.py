# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Axiom ``webapp`` extension — serves a versioned ``/api/v1`` JSON API.

The backend rides the shared HTTP substrate (the ``http`` builtin): it
contributes a single :class:`~axiom.extensions.builtins.http.registry.MountSpec`
to the composed app, so ``axi serve`` exposes ``/api/v1`` alongside every
other mount. The API is deliberately framework-agnostic and split from the
frontend: the reference UI (a Vite multi-page React app) is built and hosted
separately and consumes this API cross-origin, exactly as the imminent mobile
app will. See ``frontend/README.md`` for the frontend contract.

Persistence, when added, goes through ``axiom.infra.db.session_for("webapp")``
(schema-per-extension, ADR-052). Human/web authentication is provided by the
shared ``axiom.webauth`` module (extracted, generalized JWT auth) and enforced
per-route via FastAPI dependencies — not by the coarse per-mount authz gate.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .mount import mount_spec

__all__ = ["mount_spec", "__version__"]
