# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""MountSpec factory — how ``webapp`` attaches to the composed HTTP app.

``discover_mounts`` imports the ``mount_spec`` entry named in
``axiom-extension.toml`` (``kind = "service"``) and registers the returned
:class:`MountSpec` on the process-global router registry; ``compose_app``
then mounts it onto the one FastAPI app. See spec-serve §4.
"""

from __future__ import annotations

from axiom.extensions.builtins.http.registry import MountSpec

from .api.routers import build_api_router

#: Serve namespace for the whole web/mobile API surface. Carried by the
#: MountSpec (the AEOS 0.1 ``service`` schema has no ``prefix`` field).
API_PREFIX = "/api/v1"


def mount_spec() -> MountSpec:
    """Return the ``/api/v1`` mount for the composed app.

    ``requires_authz`` is ``False`` on purpose: authentication is enforced
    *per route* via ``axiom.webauth`` FastAPI dependencies (bearer JWT +
    ``X-API-Key``), not by the substrate's coarse per-mount gate. Leaving the
    mount gate off also lets it compose on today's ``origin/main`` without a
    globally wired authz hook (which would otherwise fail-close the mount).
    """
    return MountSpec(
        prefix=API_PREFIX,
        router=build_api_router(),
        extension="webapp",
        requires_authz=False,
        profiles=("server",),
    )
