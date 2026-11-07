# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chart's catalog: the charts a person saved, each by its stable catalog id (MVP-03-A2).

The Charts page and a chat answer's "Save" both keep their groups in one saved
view (``/api/v1/views/groups/last``: ``{"groups": [{"name", "graphs": [{"id",
"spec", ...}]}]}``). The views store holds that document opaquely; this is the
reader that knows its shape, because charts is the consumer that writes it.

A chart is listed by :func:`unified_spec.catalog_id` of its spec, the same key the
browser stamps before saving, so the page, ``axi charts saved`` and the MCP tool
open one chart by one id and draw it with the one renderer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The saved view the Charts page and chat's Save keep their groups in.
GROUPS_SURFACE = "groups"


def _document(principal: str) -> Mapping[str, Any]:
    from axiom.extensions.builtins.webapp.views import read_view

    return read_view(principal, GROUPS_SURFACE) or {}


def listing(principal: str) -> list[dict[str, Any]]:
    """Every saved chart this principal holds, in group order: id, group, graph, title, kind."""
    from axiom.extensions.builtins.scidisplay.unified_spec import catalog_id

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in _document(principal).get("groups") or []:
        if not isinstance(group, Mapping):
            continue
        for graph in group.get("graphs") or []:
            spec = graph.get("spec") if isinstance(graph, Mapping) else None
            if not isinstance(spec, Mapping):
                continue  # a channel graph the page draws from the catalog, not a spec
            cid = catalog_id(spec)
            if cid in seen:
                continue
            seen.add(cid)
            out.append(
                {
                    "id": cid,
                    "group": str(group.get("name") or ""),
                    "graph": str(graph.get("id") or ""),
                    "title": str(spec.get("title") or graph.get("name") or ""),
                    "kind": str(spec.get("kind") or ""),
                }
            )
    return out


def spec_for(principal: str, chart_id: str) -> dict[str, Any] | None:
    """The saved spec with this catalog id, or None."""
    from axiom.extensions.builtins.scidisplay.unified_spec import catalog_id

    for group in _document(principal).get("groups") or []:
        for graph in (group.get("graphs") or []) if isinstance(group, Mapping) else []:
            spec = graph.get("spec") if isinstance(graph, Mapping) else None
            if isinstance(spec, Mapping) and catalog_id(spec) == chart_id:
                return dict(spec)
    return None


__all__ = ["GROUPS_SURFACE", "listing", "spec_for"]
