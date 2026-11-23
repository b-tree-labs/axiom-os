# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.tombstone``: a person removes the content of a sealed, archived period (unit LD-6).

A tombstone is a signature, so this skill is CLI only and ends at the prompt as ``attest.seal``
does: the card (the period, the rule, how many entries lose their content, which attachments
and which archived package go) is printed, the person answers ``sign``, ``hold`` or ``ask``, and
with no one at the prompt nothing is signed. The report, the dry run, the proposal and the
switch live in ``retention.py``, because this module imports the signing prompt and so may
never be reached from an agent surface.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import service
from ..service import AttestRefused
from ._common import fail, resolve_site
from .sign import _respond


def tombstone(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Draft a tombstone of the named archive's period and sign it at the prompt."""
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was removed")
    site, err = resolve_site(params)
    if err:
        return fail(err)
    for key in ("logbook", "archive_id"):
        if not params.get(key):
            return fail(f"{key} is required")
    try:
        draft_id = service.create_tombstone_draft(site_id=site, logbook=params["logbook"],
                                                  archive_id=params["archive_id"], origin="human",
                                                  for_principal=ctx.principal.handle)  # fmt: skip
    except AttestRefused as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)


__all__ = ["tombstone"]
