# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.logbook_list`` and ``attest.logbook_validate``: read-only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillResult

from .. import registry
from ..logbooks import LogbookError, load_logbook
from ._common import fail


def _summary(logbook) -> dict[str, Any]:
    return {
        "id": logbook.id,
        "version": logbook.version,
        "display": logbook.display,
        "posture": logbook.posture,
        "not_yet_enforced": list(logbook.not_yet_enforced),
        "types": [
            {"id": t.id, "meanings": list(t.meanings), "roles": list(t.roles), "posture": t.posture}
            for t in logbook.types
        ],
    }


def list_logbooks(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    try:
        logbooks = registry.all_logbooks()
    except LogbookError as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value={"logbooks": [_summary(b) for b in logbooks]})


def validate(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    path = params.get("path")
    if not path:
        return fail("give the logbook file to validate")
    try:
        logbook = load_logbook(Path(path))
    except (LogbookError, OSError) as exc:
        return fail(f"{path}: {exc}")
    except ValueError as exc:  # tomllib.TOMLDecodeError
        return fail(f"{path}: not valid TOML: {exc}")
    return SkillResult(ok=True, value=_summary(logbook))
