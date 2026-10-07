# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi config show --effective <ext>`` — ADR-065 PR-1.

Prints the effective merged config (defaults from the registry +
current values). PR-3 will add queued-not-yet-applied surfacing for
non-reloadable fields.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.config.registry import get_registry
from axiom.infra.skills import SkillContext, SkillResult


def show_effective(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``(params, ctx) -> SkillResult``.

    Required params:
      - ``extension``: extension namespace to filter on (str).
    """
    extension = params.get("extension")
    if not extension:
        return SkillResult(
            ok=False,
            errors=["show: 'extension' is required"],
        )

    reg = get_registry()
    prefix = f"{extension}."
    effective: dict[str, Any] = {}
    for field in reg.fields():
        if not field.name.startswith(prefix):
            continue
        leaf = field.name[len(prefix):]
        effective[leaf] = reg.get(field.name)

    if not effective:
        return SkillResult(
            ok=False,
            errors=[
                f"show: no fields registered for extension {extension!r}. "
                "Call register_schema or register_schema_from_jsonschema first."
            ],
        )

    return SkillResult(
        ok=True,
        value={"extension": extension, "effective": effective},
        actions_taken=[f"read {len(effective)} fields"],
    )


__all__ = ["show_effective"]
