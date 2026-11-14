# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.cosign``: a second person co-signs a signed entry at the CLI (unit L4).

A co-signature is a signature, so this skill is CLI only and ends at the prompt as
``attest.new`` does: the card is printed, the person answers ``sign``, ``hold`` or ``ask``, and
with no one at the prompt nothing is signed. A type whose ``cosign`` declares a freshness
window needs a signing grant and so is co-signed on the web, not here. The read,
``attest.cosign_pending``, lives with the other reads in ``read.py``, because this module imports
the signing prompt and so may never be reached from an agent surface.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import service
from ..service import AttestRefused
from ._common import fail, resolve_site
from .sign import _respond


def cosign(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Co-sign a signed entry and sign the co-signature at the prompt."""
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was signed")
    target = params.get("attestation_id")
    if not target:
        return fail("attestation_id is required: the signed entry to co-sign")
    rec = service.record(target)
    if rec is None:
        return fail(f"no record {target}")
    site, err = resolve_site({"site": params.get("site") or rec["site_id"]})
    if err:
        return fail(err)
    try:
        draft_id = service.create_cosign_draft(
            site_id=site, target=target, origin="human", for_principal=ctx.principal.handle
        )
    except AttestRefused as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)
