# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.supplement``: a person corrects, clarifies or adds to a signed entry at the CLI.

The original is never changed. The supplement is its own record, signed the way ``attest.new``
signs: the card is printed, the person answers ``sign``, ``hold`` or ``ask`` at the prompt, and
with no one at the prompt nothing is signed. CLI only (ADR-142); software proposes a supplement
with ``attest.supplement_propose`` and a person signs it here or on the confirm card (unit L2).
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import service
from ..service import AttestRefused
from ._common import fail, parse_fields, resolve_site
from .sign import _respond


def content_of(params: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    content: dict[str, Any] = {"fields": fields}
    for key in ("title", "body"):
        if params.get(key):
            content[key] = params[key]
    return content


def supplement(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Supplement a signed entry and sign the supplement at the prompt."""
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was signed")
    target = params.get("attestation_id")
    if not target:
        return fail("attestation_id is required: the signed entry to supplement")
    rec = service.record(target)
    if rec is None:
        return fail(f"no record {target}")
    site, err = resolve_site({"site": params.get("site") or rec["site_id"]})
    if err:
        return fail(err)
    try:
        draft_id = service.create_supplement_draft(
            site_id=site,
            target=target,
            reason=params.get("reason"),
            content=content_of(params, parse_fields(params.get("fields"))),
            origin="human",
            for_principal=ctx.principal.handle,
        )
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)
