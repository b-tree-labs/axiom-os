# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``receipts.receipt`` — one entity's evidence record, verbatim.

Read-only. v1 resolves node-kind receipts from the fleet store (the
first oversight source); other entity kinds return a stated
not-yet-wired refusal rather than a guess.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    entity_kind = str(params.get("entity_kind", "node"))
    entity_id = str(params.get("entity_id", "")).strip()
    claim_kind = str(params.get("claim_kind", "")).strip()
    if not entity_id or not claim_kind:
        return SkillResult(ok=False, errors=["entity_id and claim_kind are required"])
    if entity_kind != "node":
        return SkillResult(
            ok=False,
            errors=[
                f"receipts for entity_kind={entity_kind!r} are not wired yet "
                "(sessions arrive with work-verification; seats with drift)"
            ],
        )

    from axiom.extensions.builtins.fleet import store as fleet_store
    from axiom.extensions.builtins.fleet.view import fleet_status

    with fleet_store.session_scope() as session:
        view = fleet_status(session)
    for node in view["nodes"]:
        if node["node_id"] == entity_id and claim_kind in node["kinds"]:
            v = node["kinds"][claim_kind]
            return SkillResult(
                ok=True,
                value={
                    "entity_kind": "node",
                    "entity_id": entity_id,
                    "claim_kind": claim_kind,
                    "site": node["site"],
                    **v,
                },
                actions_taken=["read one receipt, verbatim"],
            )
    return SkillResult(ok=False, errors=[f"no receipt for {entity_id}/{claim_kind} in scope"])


__all__ = ["run"]
