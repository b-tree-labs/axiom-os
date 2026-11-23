# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.ack``: a person acknowledges a signed entry (unit L3).

An acknowledgement is a signature ("I have seen this and it stands"), so this skill is CLI
only and ends at the prompt as ``attest.new`` does: the card is printed, the person answers
``sign``, ``hold`` or ``ask``, and with no one at the prompt nothing is signed. The read,
``attest.unacknowledged``, lives with the other reads in ``read.py``, because this module
imports the signing prompt and so may never be reached from an agent surface. Not the inbox's
``notifications.ack``, which marks an alert as taken.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import service
from ..service import AttestRefused
from ._common import fail, resolve_site
from .sign import _respond


def ack(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Acknowledge a signed entry and sign the acknowledgement at the prompt."""
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was signed")
    target = params.get("attestation_id")
    if not target:
        return fail("attestation_id is required: the signed entry to acknowledge")
    rec = service.record(target)
    if rec is None:
        return fail(f"no record {target}")
    site, err = resolve_site({"site": params.get("site") or rec["site_id"]})
    if err:
        return fail(err)
    try:
        draft_id = service.create_ack_draft(
            site_id=site, target=target, origin="human", for_principal=ctx.principal.handle
        )
    except AttestRefused as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)
