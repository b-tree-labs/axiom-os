# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for attest skills."""

from __future__ import annotations

from typing import Any

from axiom.infra.site_scope import deployment_sites
from axiom.infra.skills import SkillResult


def fail(message: str) -> SkillResult:
    return SkillResult(ok=False, value=None, errors=[message])


def resolve_site(params: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(site, error)``. A node that serves one site defaults to it; a site
    outside what the node serves is refused."""
    served = deployment_sites()
    site = params.get("site")
    if site:
        if served is not None and site not in served:
            return None, f"this node does not serve site {site!r} (serves {sorted(served)})"
        return site, None
    if served is not None and len(served) == 1:
        return next(iter(served)), None
    return None, "name the site with --site"


def parse_fields(pairs: list[str] | None) -> dict[str, Any]:
    """``["k=v", ...]`` to a dict. ``true``/``false`` become booleans; every
    other value stays text, because a number typed at a prompt is kept as
    entered (no floats in signed content)."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"field must be key=value, got {pair!r}")
        out[key] = {"true": True, "false": False}.get(value.lower(), value)
    return out
