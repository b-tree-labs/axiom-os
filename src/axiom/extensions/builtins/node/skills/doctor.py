# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``node.doctor``: the platform's health checks with their fixes, as one list."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ._handoff import UPDATE_CHECK, next_step

_SEVERITY = {"ok": "ok", "warning": "warning", "error": "error", "skipped": "skipped"}


def gather(check: str | None = None) -> dict[str, Any]:
    """Run ``axiom.cli.doctor``'s checks (the install doctor's set, now including the
    local-model alignment check) and return them as findings with fixes."""
    from axiom.cli.doctor import default_checks, run_checks

    checks = list(default_checks())
    if check:
        wanted = check.strip().lower()
        checks = [c for c in checks if c.name.lower() == wanted or wanted in c.name.lower()]
        if not checks:
            return {
                "findings": [],
                "summary": f"no check named {check!r}",
                "names": [c.name for c in default_checks()],
            }
    results = run_checks(checks)
    findings = []
    for c, r in zip(checks, results, strict=False):
        findings.append(
            {
                "name": c.name,
                "category": c.category,
                "severity": _SEVERITY.get(r.status.value, r.status.value),
                "message": r.summary,
                "fix": r.fix_hint,
                "detail": r.detail,
            }
        )
    bad = [f for f in findings if f["severity"] in ("error", "warning")]
    summary = (
        "every check passed"
        if not bad
        else f"{len(bad)} check(s) need attention: " + "; ".join(f["name"] for f in bad)
    )
    return {
        "findings": findings,
        "summary": summary,
        "errors": sum(1 for f in findings if f["severity"] == "error"),
        "warnings": sum(1 for f in findings if f["severity"] == "warning"),
    }


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    value = gather(params.get("check"))
    steps = []
    names = " ".join(
        f["name"].lower() for f in value["findings"] if f["severity"] in ("error", "warning")
    )
    if "pin" in names or "routing" in names:
        steps.append(
            next_step(
                "node.drift",
                "see every place the node disagrees with itself (a pin or configuration check failed)",
            )
        )
    if "package" in names or "coherence" in names:
        steps.append(
            next_step(
                UPDATE_CHECK,
                "is a newer release available, and what would it change (a package is behind)",
            )
        )
    return SkillResult(
        ok=True,
        value=value,
        actions_taken=[f"ran {len(value['findings'])} health check(s)"],
        next=steps,
    )


__all__ = ["gather", "run"]
