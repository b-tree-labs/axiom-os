# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``update.apply``: ``axi update`` as a capability. Chat's approval gate confirms before it runs."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.update.cli import Updater

    from .update_check import results_to_value

    try:
        results = Updater(dry_run=False).update_all(pull=False)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"the update failed: {type(exc).__name__}: {exc}"])
    value = results_to_value(results, applied=True)
    return SkillResult(
        ok=not value["failed"],
        value=value,
        errors=[value["text"]] if value["failed"] else [],
        actions_taken=[f"update: {r['step']}" for r in value["steps"] if r["changed"]],
    )


__all__ = ["run"]
