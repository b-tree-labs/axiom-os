# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Composition of the ``/api/v1`` router.

The router carries the ``/api/v1`` prefix itself; ``compose_app`` mounts it
without re-prefixing. Sub-surfaces (auth, resources) are added by including
their routers here as they land — keeping one obvious place where the API
shape is assembled.
"""

from __future__ import annotations

from fastapi import APIRouter


def build_api_router() -> APIRouter:
    """Assemble the versioned API router mounted at ``/api/v1``."""
    router = APIRouter(prefix="/api/v1")
    _register_system_routes(router)
    # Future surfaces (each its own module, included here):
    #   router.include_router(build_auth_router())      # /api/v1/auth/*
    #   router.include_router(build_<resource>_router()) # /api/v1/<resource>/*
    return router


def _register_system_routes(router: APIRouter) -> None:
    """Liveness + build-identity endpoints (public, no auth)."""

    @router.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "webapp"}

    @router.get("/version", tags=["system"])
    def version() -> dict[str, str]:
        # Lazy import avoids a package-load-time cycle with __init__.
        from axiom.extensions.builtins.webapp import __version__

        return {"service": "webapp", "version": __version__, "api": "v1"}
