# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.seal``: a person closes a period of a log at the CLI (unit L5).

A close-out is a signature, so this skill is CLI only and ends at the prompt as ``attest.new``
does: the card (the covered range, the open items the server found, the chain head and the
person's summary) is printed, the person answers ``sign``, ``hold`` or ``ask``, and with no one
at the prompt nothing is sealed. A seal whose declaration has a freshness window needs a signing
grant and so is sealed on the web, not here. The reads and the proposal (``attest.seal_preview``,
``attest.seal_propose``) live in ``read.py`` and ``propose.py``, because this module imports the
signing prompt and so may never be reached from an agent surface.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import service
from ..service import AttestRefused
from ._common import fail, resolve_site
from .sign import _respond


def seal(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Close out a period of a log and sign the seal at the prompt."""
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was sealed")
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    try:
        draft_id = service.create_seal_draft(
            site_id=site,
            logbook=params["logbook"],
            boundary=params.get("boundary") or None,
            summary=params.get("summary") or "",
            origin="human",
            for_principal=ctx.principal.handle,
            from_seq=params.get("from_seq"),
            to_seq=params.get("to_seq"),
        )
    except AttestRefused as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)
