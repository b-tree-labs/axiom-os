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
    _register_catalog_routes(router)
    _register_contributed_verbs(router)
    return router


def _register_contributed_verbs(router: APIRouter) -> None:
    """Let installed extensions add their slice of /api/v1.

    This is what makes the surface unified without making it coupled: a verb
    declares `kind = "api"` in its own manifest and is discovered here, so this
    module imports no verb and installing one verb does not drag in the rest.

    Discovery touching the filesystem must never be the reason the API fails to
    come up — a health probe and a docs render both build this router — so a
    discovery failure costs the contributed verbs and leaves the rest serving.
    """
    try:
        from axiom.extensions.builtins.webapp.api.contributions import (
            apply_contributions,
            collect_contributions,
        )
        from axiom.extensions.discovery import discover_extensions

        contributions = collect_contributions(discover_extensions())
    except Exception:  # noqa: BLE001 - the core API still serves
        import logging

        logging.getLogger(__name__).exception(
            "could not collect api contributions; /api/v1 serves its built-in "
            "routes only and every contributed verb will 404"
        )
        return

    apply_contributions(router, contributions)


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


def _register_catalog_routes(router: APIRouter) -> None:
    """The serving catalog: /api/v1/sites and /api/v1/sites/{site}/channels.

    The database is touched per request inside the handlers, not here, so these
    routes exist even where no database does — a health probe and a docs render
    both build this router.
    """
    from axiom.extensions.builtins.webapp.api.catalog import register_catalog_routes

    register_catalog_routes(router)
