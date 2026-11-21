# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tab values for ``<cli> log <log>``: the handles of the logs on this node (ADR-243), and for
``--where`` the logs' declared filters and the values a reader may pick (ADR-268 D3, D5).

Filled from ``attest.logbook_list``, the skill ``<cli> log list`` calls, so Tab offers exactly
the logs the list shows; and from ``attest.filters`` at the bound site, so a value offered is
one from entries at that site, never anybody else's.
"""

from __future__ import annotations

import logging
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

#: What ``--where`` completes to for each control: the operators a filter takes.
_OPERATORS = {"choice": ("=",), "values": ("=",), "checkbox": ("=",), "words": ("~",),
              "range": (">=", "<=")}  # fmt: skip


def _ctx() -> SkillContext:
    from axiom.infra.paths import get_user_state_dir

    from .skills import bind_default

    return SkillContext(registry=bind_default(), state_dir=get_user_state_dir(),
                        logger=logging.getLogger("axi.log"))  # fmt: skip


def _sites(pctx: Any) -> list[str]:
    """The site whose values Tab offers: the bound one, else the one this node serves."""
    site = (getattr(pctx, "binding", None) or {}).get("site")
    if site:
        return [site]
    from axiom.infra.site_scope import deployment_sites

    served = deployment_sites()
    return sorted(served) if served and len(served) == 1 else []


def _where(ctx: SkillContext, book: dict[str, Any], sites: list[str]) -> list[str]:
    out: list[str] = []
    for f in book.get("filters") or []:
        out += [f"{f['field']}{op}" for op in _OPERATORS.get(f.get("control"), ())]
        if f.get("control") == "checkbox":
            out += [f"{f['field']}=yes", f"{f['field']}=no"]
    for site in sites:
        params = {"logbook": book["id"], "site": site}
        try:
            result = invoke_capability(ctx.registry, "attest.filters", params, ctx, surface=CLI_SURFACE)
        except Exception:  # noqa: BLE001 - Tab never fails
            continue
        if not result.ok or not isinstance(result.value, dict):
            continue
        for f in result.value.get("filters") or []:
            out += [f"{f['field']}={v['value']}" for v in f.get("values") or []]
    return out


def collect(pctx: Any) -> dict[str, list[str]]:
    """``{"log": [handle, ...], "log_filter": [...]}``; nothing when the logs cannot be read."""
    ctx = _ctx()
    try:
        result = invoke_capability(ctx.registry, "attest.logbook_list", {}, ctx, surface=CLI_SURFACE)
    except Exception:  # noqa: BLE001 - Tab never fails
        return {}
    if not result.ok or not isinstance(result.value, dict):
        return {}
    books = result.value.get("logbooks") or []
    sites = _sites(pctx)
    filters: list[str] = []
    for b in books:
        if b.get("filters"):
            filters += _where(ctx, b, sites)
    return {"log": [b.get("handle") or b["id"] for b in books],
            "log_filter": list(dict.fromkeys(filters))}


__all__ = ["collect"]
