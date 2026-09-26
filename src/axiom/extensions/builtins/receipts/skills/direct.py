# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``receipts.direct`` — read, set, or PROPOSE the day-focus directive.

R12/K2 with the ADR-114 posture: a gated surface (CLI on the node, the
web behind the gate) may SET; the MCP courier may only PROPOSE — a
proposed focus is recorded with provenance and never displaces an
active one until confirmed. Every change is receipted by construction
(the row IS the receipt: text, set_by, state, set_at).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import store
from ..db_models import Focus


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    site = str(params.get("site", "") or "")
    text = params.get("text")
    mode = str(params.get("mode", "get"))  # get | set | propose
    set_by = str(params.get("set_by", "") or "@unknown")

    with store.session_scope() as session:
        row = session.get(Focus, site)
        if mode == "get" or text is None:
            return SkillResult(
                ok=True,
                value={
                    "focus": {
                        "text": row.text,
                        "set_by": row.set_by,
                        "state": row.state,
                        "set_at": row.set_at.isoformat(),
                    }
                    if row
                    else None
                },
                actions_taken=["read focus"],
            )

        if mode == "propose" and row is not None and row.state == "active":
            # A proposal never displaces an active human-set focus; it is
            # surfaced beside it (the brief renders PROPOSED distinctly).
            return SkillResult(
                ok=False,
                errors=[
                    "an active focus exists; a proposal cannot displace it — "
                    "confirm through a gated surface (CLI/web) to change it"
                ],
                value={"active": row.text, "active_set_by": row.set_by},
            )

        state = "active" if mode == "set" else "proposed"
        if row is None:
            row = Focus(
                site=site, text=str(text), set_by=set_by, state=state, set_at=datetime.now(UTC)
            )
            session.add(row)
        else:
            row.text = str(text)
            row.set_by = set_by
            row.state = state
            row.set_at = datetime.now(UTC)
        session.commit()
        return SkillResult(
            ok=True,
            value={"focus": {"text": row.text, "set_by": row.set_by, "state": state}},
            actions_taken=[f"focus {state} by {set_by}"],
        )


__all__ = ["run"]
