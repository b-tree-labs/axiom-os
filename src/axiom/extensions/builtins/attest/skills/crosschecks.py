# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.crosschecks`` (read): a log's cross-check facts and predicate states (#1816)."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import crosscheck
from ..logbooks import LogbookError
from ._common import fail, refuse_filter, resolve_site


def crosschecks(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("name the log: logbook")
    try:
        body = crosscheck.view(site, params["logbook"], result=params.get("result") or None,
                               attestation_id=params.get("attestation_id") or None)  # fmt: skip
    except crosscheck.FilterError as exc:
        return refuse_filter(str(exc))
    except LogbookError as exc:
        return fail(str(exc))
    from .. import reads

    reads.note_access(
        what="crosschecks", site=site, logbook=params["logbook"], count=len(body["crosschecks"]),
        surface=getattr(ctx, "surface", None),
        person=getattr(getattr(ctx, "principal", None), "handle", None),
    )  # fmt: skip
    return SkillResult(ok=True, value=body)
