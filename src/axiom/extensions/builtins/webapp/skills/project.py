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
    """Publish the catalog from gold (W13): refresh when gold moved, ``full`` re-baselines, ``rebuild`` starts over.

    Every run records the gold version in ``publication_state``. A source
    outside gold is refused before anything runs: silver never serves.
    """
    from axiom.extensions.builtins.webapp.catalog import publish, store, sync

    source = params.get("source") or publish.CATALOG_SOURCE
    full = bool(params.get("full", False))
    rebuild = bool(params.get("rebuild", False))
    try:
        publish.require_gold(source)
    except publish.SilverNeverServes as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    try:
        with store.session_scope() as session:
            if source != publish.CATALOG_SOURCE:
                # A different gold object, by hand: projected, not a registered publication.
                sync.project_catalog(session, source=source, full=full)
                outcome: dict[str, Any] = {"app_table": "site_catalog_channel", "gold_object": source}
            elif rebuild:
                outcome = publish.rebuild(session, "site_catalog_channel")[0]
            elif full:
                outcome = publish.publish(session, "site_catalog_channel", full=True)
            else:
                outcome = publish.refresh(session, "site_catalog_channel")
            summaries = store.read_site_summaries(session)
            fresh = publish.freshness(session, "site_catalog_channel") if source == publish.CATALOG_SOURCE else None
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return SkillResult(
            ok=False,
            errors=[f"catalog projection failed against {source}: {exc}"],
        )

    said = "gold unchanged; the catalog is current" if outcome.get("unchanged") else (
        f"published {source} into the serving catalog")
    return SkillResult(
        ok=True,
        value={
            "sites": len(summaries),
            "channels": sum(s["channels"] for s in summaries),
            "rows": sum(s["rows"] for s in summaries),
            "source": source,
            "full": full,
            "rebuild": rebuild,
            "publication": outcome,
            "freshness": fresh,
        },
        actions_taken=[said],
    )


__all__ = ["run"]
