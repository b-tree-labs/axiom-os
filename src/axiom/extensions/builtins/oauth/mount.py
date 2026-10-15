# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MountSpec factories — how ``oauth`` attaches to the composed HTTP app.

Two **public** mounts (``requires_authz=False``); both must be reachable before a
client holds any token, and the token endpoint authenticates the client from the
request itself, not via GUARD. See spec-serve §4, ADR-082.

- :func:`mount_spec` — discovery + JWKS at ``/.well-known``.
- :func:`endpoints_mount_spec` — the token endpoint at ``/oauth``.
"""

from __future__ import annotations

from axiom.extensions.builtins.http.registry import MountSpec

from .api.routers import build_oauth_endpoints_router, build_oauth_router

#: Namespace claim for the public discovery + JWKS routes.
WELL_KNOWN_PREFIX = "/.well-known"
#: Namespace claim for the token / authorize endpoints.
OAUTH_PREFIX = "/oauth"


def mount_spec() -> MountSpec:
    """Return the public ``/.well-known`` discovery + JWKS mount."""
    return MountSpec(
        prefix=WELL_KNOWN_PREFIX,
        router=build_oauth_router(),
        extension="oauth",
        requires_authz=False,  # discovery + public JWKS must be reachable unauthenticated
        profiles=("server",),
    )


def endpoints_mount_spec() -> MountSpec:
    """Return the public ``/oauth`` token-endpoint mount.

    Public because the token endpoint authenticates the *client* from the request
    (Basic / private_key_jwt) — it is not a GUARD-protected resource.
    """
    return MountSpec(
        prefix=OAUTH_PREFIX,
        router=build_oauth_endpoints_router(),
        extension="oauth",
        requires_authz=False,
        profiles=("server",),
    )
