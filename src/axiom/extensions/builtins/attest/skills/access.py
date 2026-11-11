# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.access``: who read a log, when, through which surface (unit X2).

Reads the one action audit chain and lists the access records, newest first. Reading the access
log is not itself recorded, which is what keeps it from recursing."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import reads
from ._common import fail


def access(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    state_dir = getattr(ctx, "state_dir", None)
    if state_dir is None:
        from axiom.infra.paths import get_user_state_dir

        state_dir = get_user_state_dir()
    try:
        rows = reads.access_records(
            state_dir,
            logbook=params.get("logbook") or None,
            since=params.get("since") or None,
            limit=int(params.get("limit") or 50),
        )
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(
        ok=True,
        value={
            "logbook": params.get("logbook") or None,
            "since": params.get("since") or None,
            "reads": rows,
        },  # fmt: skip
    )
