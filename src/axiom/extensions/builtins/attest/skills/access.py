# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.access``: who read a log, when, through which surface (unit X2).

Reads the one action audit chain and lists the access records, newest first. It is a door
(LD-1, #2179): the ids and counts it returns obey the same site and log rules as the records
they name, and its own read is recorded after its answer, so a read never lists itself."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import reads
from ._common import fail


def _readable(rows: list[dict[str, Any]], params: dict[str, Any], ctx: Any) -> list[dict[str, Any]]:
    """Keep the reads of the admitted site and of logs this caller may read, each by the one
    check (LD-1-A5): the ids and counts of a read say what exists, so they follow the same rules
    as the records. A read with no log or no site (a search, a decision) names nothing readable."""
    from .. import doors

    a = doors.current()
    caller = a.caller if a is not None else doors.caller_from_ctx(ctx)
    site = params.get("site")
    seen: dict[tuple[str, str], bool] = {}
    kept = []
    for row in rows:
        key = (str(row.get("site") or ""), str(row.get("logbook") or ""))
        if not key[0] or not key[1] or (site and key[0] != site):
            continue
        if key not in seen:
            seen[key] = doors.readable(caller, "attest.access", key[0], key[1])
        if seen[key]:
            kept.append(row)
    return kept


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
    rows = _readable(rows, params, ctx)
    reads.note_access(
        what="access",
        site=params.get("site"),
        logbook=params.get("logbook") or None,
        count=len(rows),
    )  # fmt: skip  (a count; the ids stay out of this read's own record)
    return SkillResult(
        ok=True,
        value={
            "logbook": params.get("logbook") or None,
            "since": params.get("since") or None,
            "reads": rows,
        },  # fmt: skip
    )
