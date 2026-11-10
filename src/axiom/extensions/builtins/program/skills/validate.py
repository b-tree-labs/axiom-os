# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.validate`` — is the data file a valid ``axiom.program/0.1``?

The agent updates the file and a human edits it by hand (prd-program R2),
so both need the same loud, complete answer: valid, or every defect at
once. This is the model's validator surfaced as a capability — the CLI
verb, an agent, and a pre-publish check all ask the same question through
the same door.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramError, ProgramValidationError, load_program
from ._source import resolve_data_path


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data_path, refusal = resolve_data_path(params, ctx)
    if refusal is not None:
        return SkillResult(ok=False, errors=[refusal])
    try:
        data = load_program(data_path)
    except ProgramValidationError as exc:
        return SkillResult(ok=False, errors=list(exc.errors))
    except ProgramError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(
        ok=True,
        value={
            "path": str(data_path),
            "schema": data.schema,
            "program": data.program.get("id"),
            "lanes": len(data.lanes),
            "people": len(data.people),
            "items": len(data.schedule),
            "bindings": sorted(data.bindings),
            # The declared canonical endpoints, so a validate pass also surfaces
            # the stable link-out / backlink targets a renderer can rely on.
            "endpoints": sorted(data.endpoints()),
        },
    )
