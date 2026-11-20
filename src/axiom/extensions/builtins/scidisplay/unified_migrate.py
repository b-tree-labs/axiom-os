# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The two older chart vocabularies, and saved views, read as ChartSpec 2.0.

Nothing that was saved stops opening (ADR-214). Each converter is pure, total
over the documents its source vocabulary admits, and lossless in the one
direction that matters: every field of the old document lands somewhere in the
new one, and a test reads each old shape and asserts it.

- :func:`from_v1` — a ChartSpec 1.x *query* document (:mod:`.chart_spec`):
  ``timeseries`` and ``comparison`` become ``line`` over ``data.query``
  (``comparison`` keeps its meaning as ``encoding.compare``), ``state`` stays
  ``state``.
- :func:`from_catalog` — a data-platform catalogue entry
  (``data_platform.charts.ChartSpec``): its registry fields become
  ``catalog``, its kind maps through :data:`CATALOG_KINDS`, and the chart is
  drawn from the gold verb it names.
- :func:`from_saved_graph` / :func:`migrate_saved_view` — a Charts page graph
  as the page saves it (``{site, series: ["feed:channel"], window, log,
  name}``), and a whole saved view of groups of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import unified_spec as us

#: Catalogue kind -> ChartSpec 2.0 kind (+ encoding). Every catalogue kind maps.
CATALOG_KINDS: dict[str, tuple[str, dict[str, Any]]] = {
    "timeseries": ("line", {}),
    "dual_axis_timeseries": ("line", {}),
    "measured_vs_predicted": ("line", {"compare": True}),
    "scatter": ("scatter", {}),
    "xy_fit": ("scatter", {"fit": "linear"}),
    "bar": ("bar", {}),
    "histogram": ("histogram", {}),
    "stacked_area": ("area", {"stack": "stacked"}),
    "scalar_panel": ("kpi", {}),
    "event_table": ("step", {}),
    "hex_map": ("heatmap", {}),
    "field_2d": ("heatmap", {}),
    "coverage": ("bar", {}),
}

_V1_KINDS = {"timeseries": ("line", {}), "comparison": ("line", {"compare": True}), "state": ("state", {})}


def _window(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {k: v for k, v in raw.items()}


def from_v1(document: Mapping[str, Any]) -> dict[str, Any]:
    """A 1.x query document as 2.0. Refuses a 2.x document (it needs no migration)."""
    version = str(document.get("schema_version") or "")
    if not version.startswith("1."):
        raise us.UnifiedSpecError([f"from_v1 reads 1.x documents, not {version or 'an unversioned one'}"])
    kind, enc = _V1_KINDS.get(str(document.get("kind")), (None, None))
    if kind is None:
        raise us.UnifiedSpecError([f"1.x kind {document.get('kind')!r} has no 2.0 equivalent registered"])
    query: dict[str, Any] = {"site": document["site"], "channels": list(document.get("channels") or [])}
    if document.get("window"):
        query["window"] = _window(document["window"])
    if document.get("transform"):
        query["transform"] = dict(document["transform"])
    out: dict[str, Any] = {
        "schema_version": us.SCHEMA_VERSION,
        "kind": kind,
        "title": document.get("title") or ", ".join(query["channels"]) or "Chart",
        "data": {"query": query},
    }
    if enc:
        out["encoding"] = dict(enc)
    for key in ("display_units", "series_colours"):
        if document.get(key):
            out[key] = dict(document[key])
    return us.parse(out)


def from_catalog(entry: Any) -> dict[str, Any]:
    """A data-platform catalogue entry (object or mapping) as 2.0."""
    get = (lambda k, d=None: entry.get(k, d)) if isinstance(entry, Mapping) else \
        (lambda k, d=None: getattr(entry, k, d))
    old = str(get("kind"))
    if old not in CATALOG_KINDS:
        raise us.UnifiedSpecError([f"catalogue kind {old!r} has no 2.0 mapping"])
    kind, enc = CATALOG_KINDS[old]
    verb = get("verb")
    params: dict[str, Any] = {}
    roles = list(get("roles") or ())
    channels = list(get("channels") or ())
    if roles:
        params["roles"] = roles
    if channels:
        params["channels"] = channels
    catalog: dict[str, Any] = {
        "group": get("group"), "order": get("order"), "default": bool(get("default")),
        "provenance": get("provenance"),
    }
    if roles:
        catalog["roles"] = roles
    if channels:
        catalog["channels"] = channels
    if verb:
        catalog["verb"] = verb
    for key in ("unit_authority", "origin", "notes"):
        if get(key):
            catalog[key] = get(key)
    if get("surfaces"):
        catalog["surfaces"] = list(get("surfaces"))
    out: dict[str, Any] = {
        "schema_version": us.SCHEMA_VERSION,
        "id": get("chart_id"),
        "kind": kind,
        "title": get("name"),
        # A chart with no verb behind it is a data gap, and says so by naming none.
        "data": {"verb": {"name": verb or "unserved", "params": params}},
        "catalog": catalog,
    }
    if get("unit"):
        out["axes"] = {"y": {"unit": get("unit")}} if us.lookup_kind(kind).cartesian else None
        if out["axes"] is None:
            out.pop("axes")
    if enc:
        out["encoding"] = dict(enc)
    return us.parse(out)


def from_saved_graph(graph: Mapping[str, Any], *, window: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One Charts-page graph, as saved, as 2.0 (a query over its series)."""
    series = [str(s) for s in graph.get("series") or []]
    if not series:
        raise us.UnifiedSpecError(["a saved graph with no series draws nothing"])
    query: dict[str, Any] = {"site": graph.get("site") or "", "channels": series}
    w = graph.get("window") or window
    if w:
        query["window"] = {"from": w["from"], "to": w["to"]}
    out: dict[str, Any] = {
        "schema_version": us.SCHEMA_VERSION,
        "kind": graph.get("kind") or "line",
        "title": graph.get("name") or ", ".join(s.split(":", 1)[-1] for s in series),
        "data": {"query": query},
    }
    if graph.get("log"):
        out["axes"] = {"y": {"type": "log"}}
    return us.parse(out)


def migrate_saved_view(view: Mapping[str, Any]) -> dict[str, Any]:
    """A saved Charts view with every graph given its 2.0 spec, nothing removed.

    The page's own state (groups, sync flags, which figure is expanded) is
    kept as it was; each graph gains ``spec``. A view saved before groups
    existed (``graphs`` at the top) is read the same way.
    """
    out = dict(view)
    if view.get("groups"):
        groups = []
        for g in view["groups"]:
            g2 = dict(g)
            g2["graphs"] = [_with_spec(gr, g.get("window") if g.get("syncDates") else None)
                            for gr in g.get("graphs") or []]
            groups.append(g2)
        out["groups"] = groups
    if view.get("graphs"):
        out["graphs"] = [_with_spec(gr, view.get("window")) for gr in view["graphs"]]
    return out


def _with_spec(graph: Mapping[str, Any], window: Mapping[str, Any] | None) -> dict[str, Any]:
    g = dict(graph)
    if g.get("series"):
        g["spec"] = from_saved_graph(graph, window=window)
    return g


__all__ = ["CATALOG_KINDS", "from_catalog", "from_saved_graph", "from_v1", "migrate_saved_view"]
