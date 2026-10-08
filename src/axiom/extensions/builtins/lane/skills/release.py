# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``lane.release`` — give a lane back.

Dropping the database is SEPARATE and opt-in. Releasing a claim is a
bookkeeping act that costs nothing to undo; dropping a database is not, and
the two should never ride on one flag. A lane released by mistake is
re-claimed in a second — a database dropped by mistake is somebody's
afternoon.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..registry import Registry
from . import lanes_path


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["which lane? pass name="])

    reg = Registry(params.get("registry") or lanes_path())
    lane = reg.release(name)
    if lane is None:
        return SkillResult(ok=False, errors=[f"no lane named {name!r}"])

    actions = [f"released lane {name} (:{lane.front}/{lane.api})"]
    value: dict[str, Any] = {"lane": name, "database": lane.database or None, "dropped": False}

    if params.get("drop_database") and lane.database:
        # Deliberately NOT executed here. Dropping a database is irreversible
        # and belongs to the operator or to TIDY's guarded, reversible
        # cleanup path (ADR-046) — not to a bookkeeping verb.
        value["drop_command"] = f"dropdb {lane.database}"
        actions.append(
            f"database {lane.database} left in place; run the printed command to drop it"
        )

    return SkillResult(value=value, actions_taken=actions)
