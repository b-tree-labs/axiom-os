# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``GET /api/v1/binding``: the one serving mode, in the shape the mode chip reads (ADR-207).

Built on :func:`axiom.infra.serving_mode.resolve_mode`, the resolver chat, document retrieval, the
CLI data reads and MCP read, so the chip cannot say something the terminal beside it does not.
The report names the site, the node's host, the key's site and the audience being viewed as. It
never carries the key, its vault reference or the node's full URL with credentials.
"""

from __future__ import annotations

from typing import Any


def binding_report() -> dict[str, Any]:
    from axiom.extensions.builtins.webapp.bound_web import WEB_READS
    from axiom.infra.serving_mode import PROBES, resolve_mode, skew, sources

    mode = resolve_mode()
    node = PROBES.get(mode)
    notes, disabled = skew(node, needed=WEB_READS)
    out: dict[str, Any] = {
        "mode": mode.mode,
        "site": mode.site,
        "node": mode.node,
        "key_site": mode.key_site,
        "view_as": mode.view_as,
        "view_as_options": list(mode.view_as_options),
        "sources": sources(mode),
        "test_data": mode.test_data,
        "node_api": {"api": node.api, "version": node.version, "min_client": node.min_client},
        "skew": notes,
        "disabled": sorted(disabled),
        "reachable": node.reachable,
        "offline": node.offline,
        "error": node.error,
        "checked_at": node.checked_at,
        "reason": mode.reason,
        "notes": list(mode.notes),
        "switch": _switch(mode.mode),
    }
    return out


def _switch(mode: str) -> list[str]:
    if mode == "bound":
        return ["site unbind (this machine's platform)", "site bind <site> (another site)",
                "axi serve --bound (this app against the binding)"]
    if mode == "test":
        return ["unset AXIOM_SAMPLE_DATA (leave test mode)"]
    if mode == "hosted":
        return ["this is the site's own app; bind a laptop to work beside it"]
    return ["site bind <site> (read a site node's data)", "axi serve --bound"]


__all__ = ["binding_report"]
