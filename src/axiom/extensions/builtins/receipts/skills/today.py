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
from ..brief import brief_payload, compose_brief, render_brief_text
from ..sources import all_claims


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    site = params.get("site")
    sites = [site] if site else None
    snapshot = bool(params.get("snapshot", True))

    from axiom.extensions.builtins.fleet import store as fleet_store

    with fleet_store.session_scope() as fsession:
        items = all_claims(fsession, sites=sites).as_items()
        with store.session_scope() as session:
            # this_node + fleet_session, as the web route and the digest
            # already pass. Without them `compose_brief` skips handling and
            # reach, so the CLI and MCP projections of this brief were a
            # POORER brief than the other two from the same composer — no
            # "why you" sentence, no reach — which is the same drift that let
            # the text projection keep speaking in claims.
            brief = compose_brief(
                session,
                items,
                site=site,
                snapshot=snapshot,
                this_node=_this_node(),
                fleet_session=fsession,
            )
            session.commit()

    return SkillResult(
        ok=True,
        value={
            "text": render_brief_text(brief),
            "brief": brief_payload(brief),
        },
        actions_taken=[
            f"composed brief: {brief.counts['cases']} case(s) waiting, "
            f"{brief.counts['deltas']} trust delta(s)" + ("; snapshot recorded" if snapshot else "")
        ],
    )


def _this_node() -> str:
    """The node this process runs on — what `handling` needs to tell a fix it
    can run here from one it cannot."""
    import os
    import platform

    return os.environ.get("AXIOM_FLEET_NODE_ID") or platform.node()


__all__ = ["run"]
