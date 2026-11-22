# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.reviews_coverage`` (read): review coverage of a log (#1817).

The one check (``doors.admitted_skill``) has admitted the caller for the reviewed log before this
runs; a review counts only from a reviews log the same caller may read.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import doors, reviews
from ..logbooks import LogbookError
from ._common import fail, resolve_site


def coverage(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("name the log: logbook")
    caller = doors.caller_from_ctx(ctx)
    try:
        body = reviews.coverage(site, params["logbook"], may_read=lambda b: doors.readable(
            caller, "attest.reviews_coverage", site, b))  # fmt: skip
    except LogbookError as exc:
        return fail(str(exc))
    from .. import reads

    reads.note_access(
        what="reviews", site=site, logbook=params["logbook"], count=body["reviews"],
        surface=getattr(ctx, "surface", None),
        person=getattr(getattr(ctx, "principal", None), "handle", None),
    )  # fmt: skip
    return SkillResult(ok=True, value=body)
