# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``doctor.run``: the environment checks ``axi doctor`` runs, as data. Changes nothing.

``axi doctor`` also asks a model to explain the failures and, with ``--fix``, remediates. A
conversation gets the checks and their fixes only: what to change is said, and the person (or a
confirmed ``update`` or ``settings`` call) changes it.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.axiom_cli import _gather_diagnostics

    try:
        checks = [dict(c) for c in _gather_diagnostics().get("checks", [])]
    except Exception as exc:  # noqa: BLE001 - a health check that raises is itself a finding
        return SkillResult(
            ok=False, errors=[f"the checks could not run: {type(exc).__name__}: {exc}"]
        )

    try:
        from .binding import report

        binding = report(probe=True)
        checks.append(
            {
                "name": "Site connection",
                "ok": binding["mode"] != "bound" or bool(binding["reachable"]),
                "status": binding["text"],
                "fix": binding.get("fix") or None,
            }
        )
    except Exception:  # noqa: BLE001 - the connection line is added, never required
        pass

    issues = [c for c in checks if not c.get("ok")]
    lines = [
        f"{'ok  ' if c.get('ok') else 'FIX '} {c['name']}: {c.get('status', '')}" for c in checks
    ]
    for c in issues:
        if c.get("fix"):
            lines.append(f"  fix for {c['name']}: {c['fix']}")
    summary = (
        "Everything checked is healthy."
        if not issues
        else f"{len(issues)} thing(s) need attention."
    )
    value: dict[str, Any] = {
        "healthy": not issues,
        "issues": len(issues),
        "checks": checks,
        "text": "\n".join([summary, *lines]),
    }
    if params.get("error"):
        value["error_reported"] = str(params["error"])
    return SkillResult(ok=True, value=value, actions_taken=["ran the environment checks"])


__all__ = ["run"]
