# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.validate`` — is the data file a valid ``axiom.program/0.1``?

The agent updates the file and a human edits it by hand (prd-program R2),
so both need the same loud, complete answer: valid, or every defect at
once. This is the model's validator surfaced as a capability — the CLI
verb, an agent, and a pre-publish check all ask the same question through
the same door.

Beyond the schema, the taxonomy check (prd-program R27) reports every open
item that breaks the Program -> Lane -> Epic -> Task convention: no lane,
more than one lane, an epic with no program, a task with no epic, a body
missing its ``Owner:`` or ``Done when:`` line, a placeholder title. Lanes are
read from ``lane:<slug>`` labels, never from a lane issue. The findings are
reported under ``taxonomy`` and do not make a schema-valid file invalid unless
``strict`` asks them to.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import entropy
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
    findings = entropy.taxonomy_findings(data)
    tax = entropy.taxonomy_config(data.program)
    taxonomy = {
        "conventions": {k: tax[k] for k in entropy.DEFAULT_TAXONOMY},
        "checked": list(entropy.TAXONOMY_KINDS),
        "findings": findings,
        "count": len(findings),
    }
    if params.get("strict") and findings:
        return SkillResult(
            ok=False,
            value={"taxonomy": taxonomy},
            errors=[f"{f['subject']}: {f['kind']}: {f['detail']}" for f in findings],
        )
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
            "taxonomy": taxonomy,
        },
    )
