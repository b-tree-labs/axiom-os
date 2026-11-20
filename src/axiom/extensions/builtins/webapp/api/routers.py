# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Composition of the ``/api/v1`` router.

The router carries the ``/api/v1`` prefix itself; ``compose_app`` mounts it
without re-prefixing. Sub-surfaces (auth, resources) are added by including
their routers here as they land — keeping one obvious place where the API
shape is assembled.
"""

from __future__ import annotations

from fastapi import APIRouter, Request


def build_api_router() -> APIRouter:
    """Assemble the versioned API router mounted at ``/api/v1``."""
    return build_api_router_and_owners()[0]


def build_api_router_and_owners() -> tuple[APIRouter, tuple[tuple[str, str], ...]]:
    """The ``/api/v1`` router, and which of its paths another extension owns.

    The owners are what lets a key scoped to the extension that declared a route
    reach it (``MountSpec.owners``, #1777).
    """
    router = APIRouter(prefix="/api/v1")
    _register_system_routes(router)
    _register_site_sign_in(router)
    _register_catalog_routes(router)
    _register_view_routes(router)
    owners = _register_contributed_verbs(router)
    return router, owners


def _owned_paths(router: APIRouter, added: dict[str, tuple[str, set[int]]]) -> tuple[tuple[str, str], ...]:
    """``(path, extension)`` for each contribution that alone serves its subpath.

    A subpath is owned only when every route under it, and every route whose
    path parameter could match a request under it, is one its own extension
    added. Otherwise a key scoped to that extension would reach a route it did
    not declare (the web app's own, or another extension's), so the subpath is
    owned by nobody and only the web app's name covers it, as before.
    """
    owners = []
    for full, (extension, own) in sorted(added.items()):
        if not own:
            continue
        under = full + "/"
        others = [getattr(r, "path", "") or "" for r in router.routes if id(r) not in own]
        intruders = [p for p in others
                     if p == full or p.startswith(under)
                     or ("{" in p and under.startswith(p.split("{", 1)[0]))]
        if intruders:
            import logging

            logging.getLogger(__name__).warning(
                "%s from %r shares its path with other routes (%s); a key scoped to %r "
                "does not reach it, only one scoped to the web app does",
                full, extension, ", ".join(sorted(set(intruders))[:3]), extension)
            continue
        owners.append((full, extension))
    return tuple(owners)


def _register_contributed_verbs(router: APIRouter) -> tuple[tuple[str, str], ...]:
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
        return ()

    added: dict[str, tuple[str, set[int]]] = {}
    for contribution in contributions:
        before = {id(r) for r in router.routes}
        if apply_contributions(router, [contribution]):
            full = router.prefix + contribution.subpath.rstrip("/")
            added[full] = (contribution.extension,
                           {id(r) for r in router.routes if id(r) not in before})
    return _owned_paths(router, added)


def _register_site_sign_in(router: APIRouter) -> None:
    """``GET /api/v1/site/sign-in``: the site's public client for local development.

    Behind the mount's gate like every non-public route here: a developer key
    (``*:read``) reads it, nothing anonymous does (#1717)."""
    from axiom.extensions.builtins.webgate.dev_client import register_route

    register_route(router)


def _register_system_routes(router: APIRouter) -> None:
    """Liveness, build identity and the serving mode (public, no auth; no secret in any of them)."""

    @router.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "webapp"}

    @router.get("/version", tags=["system"])
    def version(request: Request) -> dict:
        """What this node runs and needs, so a bound client can compare (ADR-207 rule 7).

        ``min_client`` is the oldest platform release that speaks this node's doors, and
        ``capabilities`` what it serves, read off the routes it actually mounted
        (:mod:`axiom.infra.node_compat`). A client older than ``min_client`` is told once, in
        words, with the command that updates it, instead of meeting a 403.
        """
        # Lazy import avoids a package-load-time cycle with __init__.
        from axiom.extensions.builtins.data_platform.read_target import (
            FOLLOWS_THE_BINDING,
            KIT_READS,
        )
        from axiom.extensions.builtins.webapp import __version__
        from axiom.infra.node_compat import advertised_capabilities, min_client
        from axiom.infra.serving_mode import client_version

        paths = [getattr(r, "path", "") for r in getattr(request.app, "routes", ())]
        return {
            "service": "webapp",
            "version": __version__,
            "api": "v1",
            "platform": client_version(),
            "min_client": min_client(),
            "capabilities": advertised_capabilities(paths),
            "data_reads": [*FOLLOWS_THE_BINDING, *KIT_READS],
        }

    @router.get("/binding", tags=["system"])
    def binding() -> dict:
        """The serving mode, for the mode chip (ADR-207). Never carries the key."""
        from axiom.extensions.builtins.webapp.binding_report import binding_report

        return binding_report()


def _register_catalog_routes(router: APIRouter) -> None:
    """The serving catalog: /api/v1/sites and /api/v1/sites/{site}/channels.

    The database is touched per request inside the handlers, not here, so these
    routes exist even where no database does — a health probe and a docs render
    both build this router.
    """
    from axiom.extensions.builtins.webapp.api.catalog import register_catalog_routes

    register_catalog_routes(router)


def _register_view_routes(router: APIRouter) -> None:
    """Saved views: what somebody had configured on a surface, per principal."""
    from axiom.extensions.builtins.webapp.api.views import register_view_routes

    register_view_routes(router)
