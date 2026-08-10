# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.lint`` — skill wrapper for the no_action_without_authz lint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..lint import check_paths


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    raw_paths = params.get("paths") or ["src/"]
    paths = [Path(p) for p in raw_paths]

    report = check_paths(paths)

    return SkillResult(
        ok=report.ok,
        value={
            "resource": "lint",
            "checked_files": report.checked_files,
            "checked_functions": report.checked_functions,
            "violations": [
                {
                    "path": v.path,
                    "function": v.function,
                    "lineno": v.lineno,
                    "reason": v.reason,
                }
                for v in report.violations
            ],
            "allowlisted": [
                {
                    "path": v.path,
                    "function": v.function,
                    "lineno": v.lineno,
                }
                for v in report.allowlisted
            ],
        },
        errors=[
            f"{v.path}:{v.lineno} {v.function}: {v.reason}"
            for v in report.violations
        ],
    )
