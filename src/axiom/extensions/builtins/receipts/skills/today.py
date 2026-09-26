# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``receipts.today`` — the oversight brief, composed deterministically.

The single renderer behind CLI, MCP courier, and (next) the web Today
view (spec-receipts-surface §1d). Read-mostly: composing also records
the snapshot that makes the NEXT brief's trust deltas honest.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import store
from ..brief import brief_payload, compose_brief, fleet_source, render_brief_text


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    site = params.get("site")
    sites = [site] if site else None
    snapshot = bool(params.get("snapshot", True))

    from axiom.extensions.builtins.fleet import store as fleet_store

    with fleet_store.session_scope() as fsession:
        items = fleet_source(fsession, sites=sites)
    with store.session_scope() as session:
        brief = compose_brief(session, items, site=site, snapshot=snapshot)
        session.commit()

    return SkillResult(
        ok=True,
        value={
            "text": render_brief_text(brief),
            "brief": brief_payload(brief),
        },
        actions_taken=[
            f"composed brief: {brief.counts['needs_you_shown']} needs-you, "
            f"{brief.counts['deltas']} trust delta(s)" + ("; snapshot recorded" if snapshot else "")
        ],
    )


__all__ = ["run"]
