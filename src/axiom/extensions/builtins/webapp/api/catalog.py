# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/api/v1`` catalog surface: what each site feeds, and its points.

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
        raise HTTPException(404, _not_found(site)) from None


def _not_found(site: str) -> str:
    """"No such site", and, for a key held to its site, why it sees only that one.

    The same words whichever site was asked for, so it confirms nothing about
    what else is hosted here.
    """
    from axiom.infra.site_authority import (
        UNSITED_KEY_REFUSAL,
        SiteNotAuthorized,
        held_site_note,
        tenant_scope,
    )

    try:
        held = tenant_scope()
    except SiteNotAuthorized:
        return f"no such site: {site}. {UNSITED_KEY_REFUSAL}"
    if held is None:
        return f"no such site: {site}"
    return f"no such site: {site}. {held_site_note(held)}"


def _forwarded(read: str, call, *, key: tuple = ()) -> dict:
    """ADR-207: answer a shared read from the bound node, or fail closed with the node named."""
    from fastapi import HTTPException

    from axiom.extensions.builtins.webapp import bound_web

    try:
        out = bound_web.forward(read, call, key=key)
    except bound_web.ForwardRefused as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return out


def _bound() -> bool:
    from axiom.extensions.builtins.webapp import bound_web

    return bound_web.forwarding()


def _marked(body: dict) -> dict:
    from axiom.extensions.builtins.webapp import bound_web

    return bound_web.mark_test_data(body)


def _health_settings() -> dict:
    """The window and oldest accepted version, from the platform's environment."""
    import os

    out: dict = {"min_version": os.environ.get("AXIOM_INTAKE_MIN_VERSION", "")}
    raw = os.environ.get("AXIOM_PARTNER_STOPPED_AFTER_H", "")
    if raw:
        out["stopped_after_s"] = float(raw) * 3600
    return out


def register_catalog_routes(router: APIRouter) -> None:
    """Attach the catalog endpoints to the versioned router."""

    @router.get("/sites", tags=["catalog"])
    def sites() -> dict:
        if _bound():
            # The node holds the listing to the key's site; nothing here widens it.
            out = _forwarded("sites", lambda lane: lane.sites())
            rows = [{"site": r.get("id") or r.get("site"), "label": r.get("label", ""),
                     "rows": r.get("rows", 0)} for r in out["value"]]
            return {"sites": rows, "served": out["served"]}
        from axiom.extensions.builtins.webapp.catalog import store
        from axiom.infra import site_scope

        scope = site_scope.resolve()
        with store.session_scope() as s:
            summaries = store.read_site_summaries(s)
        # Filter the listing rather than the query: the projection is shared,
        # and a name shown here that would be refused on open discloses exactly
        # what the refusal is meant to withhold.
        permitted = set(scope.filter([row["site"] for row in summaries]))
        return _marked({"sites": [row for row in summaries if row["site"] in permitted]})

    @router.get("/sites/{site}/channels", tags=["catalog"])
    def channels(site: str) -> dict:
        if _bound():
            out = _forwarded("channels", lambda lane: lane.channels(site), key=(site,))
            return {"site": site, "channels": out["value"], "served": out["served"]}
        from axiom.extensions.builtins.webapp.catalog import store

        _require_site(site)
        with store.session_scope() as s:
            return _marked({"site": site, "channels": store.read_channels(s, site)})

    @router.get("/sites/{site}/nodes", tags=["catalog"])
    def nodes(site: str) -> dict:
        """The nodes that send this site's data, and whether they are alive.

        From each node's heartbeat (ADR-164): proof of life that does not depend
        on there being data to send.
        """
        from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import (
            HeartbeatStore,
            liveness,
        )

        _require_site(site)
        out = []
        for node in HeartbeatStore.from_env().nodes(site):
            out.append({**node, "state": liveness(node)})
        return {"site": site, "nodes": out}

    @router.get("/sites/{site}/health", tags=["catalog"])
    def site_health(site: str) -> dict:
        """The site's health from its nodes' heartbeats: heard, fresh, lanes, version, disk."""
        from axiom.extensions.builtins.data_platform import partner_health

        _require_site(site)
        return partner_health.site_health(site, **_health_settings())

    @router.get("/sites/{site}/uptime", tags=["catalog"])
    def site_uptime(site: str, days: float = 7) -> dict:
        """Downtime per installation, each interval attributed to a cause (ADR-182 D5a)."""
        from axiom.extensions.builtins.data_platform import uptime

        _require_site(site)
        return uptime.site_report(site, period_s=max(1.0, min(days, 90)) * 86400)

    @router.get("/health/sites", tags=["catalog"])
    def every_site_health() -> dict:
        """Every site this request may see, worst first. A partner sees their own."""
        from axiom.extensions.builtins.data_platform import partner_health
        from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
        from axiom.infra import site_scope

        store = HeartbeatStore.from_env()
        names = sorted(p.name for p in store.root.iterdir() if p.is_dir()) if store.root.is_dir() else []
        permitted = site_scope.resolve().filter(names)
        rank = {"fail": 0, "warn": 1, "info": 2, "ok": 3}
        out = [partner_health.site_health(n, store=store, **_health_settings()) for n in permitted]
        out.sort(key=lambda h: (rank.get(h["state"], 2), h["site"]))
        return {"sites": out}

    @router.get("/sites/{site}/topology", tags=["catalog"])
    def topology_view(site: str) -> dict:
        """Every hop from this site's sending nodes to the page being read."""
        from axiom.extensions.builtins.data_platform import partner_health
        from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
        from axiom.infra import topology

        _require_site(site)
        store = HeartbeatStore.from_env()
        topo = topology.build(
            site,
            store.nodes(site),
            here=topology.this_node(),
            planned=topology.planned_for(site),
        )
        # Each reporting hop carries its health (W8): the same checks the site
        # card and the alerts use, so the three never disagree.
        return partner_health.annotate_topology(
            topo, partner_health.site_health(site, store=store, **_health_settings())
        )

    @router.get("/sites/{site}/checklist", tags=["catalog"])
    def site_checklist(site: str) -> dict:
        """ADR-178 D4: every readiness check, each ok, waiting, failing or not applicable."""
        from axiom.extensions.builtins.sites import checklist

        _require_site(site)
        return checklist.report(site, **_health_settings())

    @router.get("/sites/{site}/series", tags=["catalog"])
    def series(
        site: str,
        feed: str,
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

        wanted = [c for c in channels.split(",") if c]
        if _bound():
            return _bound_series(site, feed, wanted, bucket_s, t_from, t_to)
        _require_site(site)
        try:
            with store.session_scope() as s:
                return _marked(series_mod.bucketed_series(
                    s,
                    site=site,
                    feed=feed,
                    channels=wanted,
                    bucket_s=bucket_s,
                    t_from=t_from,
                    t_to=t_to,
                ))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    _register_lane_reads(router)


def _parse_instant(value: str | None):
    from datetime import UTC, datetime

    from fastapi import HTTPException

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"not an ISO 8601 instant: {value!r}") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _span(value: str | None):
    """``24h`` / ``7d`` / ``30m`` as a timedelta, or None."""
    from datetime import timedelta

    from fastapi import HTTPException

    if not value:
        return None
    unit = value[-1:].lower()
    try:
        n = float(value[:-1])
    except ValueError:
        raise HTTPException(422, f"not a span: {value!r} (say 24h, 7d or 30m)") from None
    scale = {"m": 60, "h": 3600, "d": 86400}.get(unit)
    if scale is None or n <= 0:
        raise HTTPException(422, f"not a span: {value!r} (say 24h, 7d or 30m)")
    return timedelta(seconds=n * scale)


def _bound_series(site, feed, wanted, bucket_s, t_from, t_to) -> dict:
    """``/series`` on a bound install: the node's bucketed means, in the local route's shape."""
    from datetime import UTC, datetime, timedelta

    from fastapi import HTTPException

    if not wanted:
        raise HTTPException(422, "channels is required (comma-separated)")
    try:
        step = int(bucket_s)
    except (TypeError, ValueError):
        raise HTTPException(422, f"bucket_s must be whole seconds, got {bucket_s!r}") from None
    if step <= 0:
        raise HTTPException(422, f"bucket_s must be positive, got {step}")
    end = _parse_instant(t_to) or datetime.now(UTC)
    start = _parse_instant(t_from) or end - timedelta(days=1)

    def call(lane):
        chans = lane.resolve(site, wanted, feed)
        got = lane.read(site, chans, start, end, step) if chans else {}
        points = []
        for c in chans:
            for t, v in got.get((c.feed, c.channel), []):
                if v is not None:
                    points.append({"channel": c.channel,
                                   "ts": t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "avg": round(float(v), 6)})
        return points

    out = _forwarded("series", call, key=(site, feed, tuple(wanted), step, t_from, t_to))
    return {"series": out["value"], "served": out["served"]}


def _register_lane_reads(router: APIRouter) -> None:
    """``dates``, ``revision`` and ``figure``: answered by the node figure lane on a bound install.

    An install that is not bound has no lane in this release; it says so (501) rather than
    drawing from wherever this machine's database happens to point.
    """
    from fastapi import HTTPException

    def _need_bound(read: str) -> None:
        if not _bound():
            from axiom.extensions.builtins.webapp.bound_web import TEST_DATA_NOTE
            from axiom.infra.serving_mode import resolve_mode

            detail = (f"`{read}` is served on a bound install (ADR-207); this install is not "
                      "bound to a site node")
            if resolve_mode().test_data:
                detail = f"{TEST_DATA_NOTE}. {detail}"
            raise HTTPException(501, detail)

    @router.get("/sites/{site}/revision", tags=["catalog"])
    def revision(site: str) -> dict:
        _need_bound("revision")
        out = _forwarded("revision", lambda lane: lane.revision(site), key=(site,))
        return {"site": site, "revision": out["value"], "served": out["served"]}

    @router.get("/sites/{site}/dates", tags=["catalog"])
    def dates(site: str, channels: str, feed: str = "") -> dict:
        _need_bound("dates")
        wanted = [c for c in channels.split(",") if c]
        out = _forwarded("dates", lambda lane: lane.dates(site, wanted, feed),
                         key=(site, feed, tuple(wanted)))
        return {"site": site, "dates": out["value"], "served": out["served"]}

    @router.get("/sites/{site}/figure", tags=["catalog"])
    def figure(
        site: str,
        channels: str,
        feed: str = "",
        t_from: str | None = None,
        t_to: str | None = None,
        last: str | None = None,
        width: int = 0,
        height: int = 0,
    ) -> dict:
        """The Figure document the appkit ``FigureSource`` draws, from the bound node's gold."""
        _need_bound("figure")
        wanted = [c for c in channels.split(",") if c]
        start, end, span = _parse_instant(t_from), _parse_instant(t_to), _span(last)
        out = _forwarded(
            "figure",
            lambda lane: lane.figure(site, wanted, feed=feed, t_from=start, t_to=end, last=span,
                                     width=max(0, min(width, 4000)),
                                     height=max(0, min(height, 4000))),
            key=(site, feed, tuple(wanted), t_from, t_to, last, width, height),
        )
        body = dict(out["value"])
        body["served"] = {**(body.get("served") or {}), **out["served"]}
        return body


__all__ = ["register_catalog_routes"]
