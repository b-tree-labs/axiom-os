# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/api/v1`` catalog surface: what each site streams, and its points.

Every route here is bounded by :mod:`axiom.infra.site_scope`. One platform
serves several sites, and a partner's deployment must behave as though it is
their site and nothing else exists — so the listing shows only what is in scope,
and a site outside it answers 404 rather than 403, because 403 would confirm
that the site exists. See that module for why the choice has to be uniform.

``/sites`` and ``/channels`` read the serving projection — counts, spans and
units, answered from an indexed table in a millisecond. ``/series`` reads gold
directly, because the projection holds no points.

Routes are registered onto the shared ``/api/v1`` router rather than included as
a sub-router, so every route stays a flat, introspectable object — the mount
test enumerates ``route.path`` to prove nothing escapes the namespace, and a
sub-router would make that check unable to see these.
"""

from __future__ import annotations

from fastapi import APIRouter


def _require_site(site: str) -> None:
    """404 for a site this request may not see.

    Not 403: that would confirm the site exists, telling one partner that
    another is hosted beside them. The omission only works if every surface
    makes it, so this raises the same way everywhere.
    """
    from fastapi import HTTPException

    from axiom.infra import site_scope

    try:
        site_scope.resolve().require(site)
    except site_scope.SiteOutOfScope:
        raise HTTPException(404, f"no such site: {site}") from None


def register_catalog_routes(router: APIRouter) -> None:
    """Attach the catalog endpoints to the versioned router."""

    @router.get("/sites", tags=["catalog"])
    def sites() -> dict:
        from axiom.extensions.builtins.webapp.catalog import store
        from axiom.infra import site_scope

        scope = site_scope.resolve()
        with store.session_scope() as s:
            summaries = store.read_site_summaries(s)
        # Filter the listing rather than the query: the projection is shared,
        # and a name shown here that would be refused on open discloses exactly
        # what the refusal is meant to withhold.
        permitted = set(scope.filter([row["site"] for row in summaries]))
        return {"sites": [row for row in summaries if row["site"] in permitted]}

    @router.get("/sites/{site}/channels", tags=["catalog"])
    def channels(site: str) -> dict:
        from axiom.extensions.builtins.webapp.catalog import store

        _require_site(site)
        with store.session_scope() as s:
            return {"site": site, "channels": store.read_channels(s, site)}

    @router.get("/sites/{site}/series", tags=["catalog"])
    def series(
        site: str,
        stream: str,
        channels: str,
        bucket_s: int,
        t_from: str | None = None,
        t_to: str | None = None,
    ) -> dict:
        """Bucketed points for a chart, flat and channel-tagged.

        Reads gold directly rather than the catalog. A bad bucket or too many
        channels is a 422 the caller can act on — never a quietly narrowed
        answer, which would render as sparse data and be indistinguishable from
        a genuinely quiet signal.
        """
        from fastapi import HTTPException

        from axiom.extensions.builtins.webapp.catalog import series as series_mod
        from axiom.extensions.builtins.webapp.catalog import store

        _require_site(site)
        wanted = [c for c in channels.split(",") if c]
        try:
            with store.session_scope() as s:
                return series_mod.bucketed_series(
                    s,
                    site=site,
                    stream=stream,
                    channels=wanted,
                    bucket_s=bucket_s,
                    t_from=t_from,
                    t_to=t_to,
                )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


__all__ = ["register_catalog_routes"]
