# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``fleet.status`` — the console view over the fleet store.

Read-only. ``params["sites"]`` (optional list) narrows to those sites;
the CLI passes user input here only on a console node, where the store
itself is scope-bounded — the HTTP surface resolves scope from the
credential instead and never trusts the query.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import store
from ..view import fleet_status


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    sites = params.get("sites")
    include_archived = bool(params.get("include_archived", False))
    with store.session_scope() as session:
        value = fleet_status(session, sites=sites, include_archived=include_archived)
    n = len(value["nodes"])
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=[f"evaluated {n} node(s) at read time"],
    )


__all__ = ["run"]
