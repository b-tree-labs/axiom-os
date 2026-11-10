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


#: The only routes under ``/api/v1`` that answer without a credential: liveness
#: and build identity, neither of which names a site, a person or a key. Every
#: other route, including any a verb contributes later, is refused before its
#: handler runs unless the request carries a verified session or key.
PUBLIC_PATHS = (f"{API_PREFIX}/health", f"{API_PREFIX}/version")

#: Routes that take a POST because their question is a structure, and change
#: nothing: a ``viewer`` (``*:read``) may draw a chart and see what to do next.
READ_PATHS = (
    f"{API_PREFIX}/charts/suggest",
    f"{API_PREFIX}/charts/render",
    f"{API_PREFIX}/next",
)


def mount_spec() -> MountSpec:
    """Return the ``/api/v1`` mount for the composed app.

    **Default-deny.** The mount gate is on: the platform's authz hook resolves
    the gate session cookie or an ``X-API-Key``/bearer key on every request,
    and binds the caller's site, which is what holds the catalog and every
    other site-bound read to that site (:mod:`axiom.infra.site_scope`). Only
    :data:`PUBLIC_PATHS` answer without one.

    The gate was off, leaving authentication to each route. The catalog routes
    did not check, so ``/api/v1/sites`` and ``/sites/{site}/channels`` listed
    every hosted site's channels, row counts and date ranges to anyone who could
    reach the port, and with no credential the site scope had nothing to narrow
    by. A route that forgets to check is now refused instead of answering.

    A node with no authz hook (no keys configured, not a loopback dev serve)
    does not serve this mount at all: compose_app's fail-closed rule.
    """
    return MountSpec(
        prefix=API_PREFIX,
        router=build_api_router(),
        extension="webapp",
        requires_authz=True,
        public=PUBLIC_PATHS,
        reads=READ_PATHS,
        profiles=("server",),
    )
