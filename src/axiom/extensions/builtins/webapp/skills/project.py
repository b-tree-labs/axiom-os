# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Refresh the serving catalog from the gold tier.

Fails closed. A projection that cannot reach its database must say so, because
the failure mode it replaces was a catalog that silently went stale while the
API kept serving yesterday's answer confidently.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult


def run(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    source = params.get("source", "gold.signals")
    full = bool(params.get("full", False))

    from axiom.extensions.builtins.webapp.catalog import store, sync

    try:
        with store.session_scope() as session:
            sync.project_catalog(session, source=source, full=full)
            summaries = store.read_site_summaries(session)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return SkillResult(
            ok=False,
            errors=[f"catalog projection failed against {source}: {exc}"],
        )

    return SkillResult(
        ok=True,
        value={
            "sites": len(summaries),
            "channels": sum(s["channels"] for s in summaries),
            "rows": sum(s["rows"] for s in summaries),
            "source": source,
            "full": full,
        },
        actions_taken=[f"projected {source} into the serving catalog"],
    )


__all__ = ["run"]
