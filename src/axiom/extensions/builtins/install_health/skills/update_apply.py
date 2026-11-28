# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``update.apply``: ``axi update`` as a capability. Chat's approval gate confirms before it runs.

Before anything is installed, the data safeguards are checked: a time-series table with no
storage rule refuses the install unless ``allow_undeclared_storage='<why>'`` is passed, and
that override is recorded. A backup that is never restored only warns.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from .doctor import data_safeguard_checks, storage_refusal

    safeguards = data_safeguard_checks(ctx)
    refusal, override = storage_refusal(params, ctx, safeguards)
    if refusal:
        return SkillResult(ok=False, errors=[refusal], value={"safeguards": safeguards})

    updater = params.get("_updater")
    try:
        if updater is None:
            from axiom.extensions.builtins.update.cli import Updater

            updater = Updater(dry_run=False)
        results = updater.update_all(pull=False)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"the update failed: {type(exc).__name__}: {exc}"])
    from .update_check import results_to_value

    value = results_to_value(results, applied=True)
    value["safeguards"] = safeguards
    if override:
        value["storage_override"] = override
    warnings = [f"{c['name']}: {c['status']}" for c in safeguards if not c["ok"]]
    if warnings:
        value["text"] = "\n".join([value.get("text", ""), *(f"WARNING {w}" for w in warnings)])
    actions = [f"update: {r['step']}" for r in value["steps"] if r["changed"]]
    if override:
        actions.append(f"installed past a storage-rule gap, recorded: {override['reason']}")
    return SkillResult(
        ok=not value["failed"],
        value=value,
        errors=[value["text"]] if value["failed"] else [],
        actions_taken=actions,
    )


__all__ = ["run"]
