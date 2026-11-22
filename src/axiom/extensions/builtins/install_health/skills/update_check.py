# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``update.check``: what an update would do. A dry run; installs nothing."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def results_to_value(results: list[Any], *, applied: bool) -> dict[str, Any]:
    rows = [
        {"step": r.step, "ok": r.success, "changed": r.changed, "message": r.message}
        for r in results
    ]
    failed = [r for r in rows if not r["ok"]]
    pending = [r for r in rows if r["changed"]]
    lines = [f"{'ok  ' if r['ok'] else 'FAIL'} {r['step']}: {r['message']}" for r in rows]
    if applied:
        head = "The update stopped on a failure." if failed else "Updated."
    else:
        head = (
            "Nothing to update."
            if not pending
            else f"{len(pending)} step(s) would change something."
        )
    return {
        "applied": applied,
        "up_to_date": not pending and not failed,
        "failed": bool(failed),
        "steps": rows,
        "text": "\n".join([head, *lines]),
    }


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.extensions.builtins.update.cli import Updater

    try:
        results = Updater(dry_run=True).check_updates()
    except Exception as exc:  # noqa: BLE001
        return SkillResult(
            ok=False, errors=[f"could not check for updates: {type(exc).__name__}: {exc}"]
        )
    return SkillResult(
        ok=True,
        value=results_to_value(results, applied=False),
        actions_taken=["checked what an update would change (dry run)"],
    )


__all__ = ["results_to_value", "run"]
