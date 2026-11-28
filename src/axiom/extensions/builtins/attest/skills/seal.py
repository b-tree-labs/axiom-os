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

from .. import registry, service
from ..logbooks import LogbookError
from ..roles import roles_for
from ..service import AttestRefused
from ._common import fail, resolve_site
from .sign import _respond


def _is_plain(logbook: Any) -> bool:
    try:
        return bool(logbook) and not registry.get(str(logbook)).signed
    except LogbookError:
        return False


def _seal_as_person(params: dict[str, Any], site: str, ctx: Any) -> SkillResult:
    """A plain log's close-out: an archival act by the person running the verb (ADR-290 D8)."""
    if getattr(ctx, "principal", None) is None:
        return fail("a close-out is made by a signed-in person; nothing was sealed")
    handle = ctx.principal.handle
    try:
        record = service.record_seal(
            site_id=site, logbook=params["logbook"], summary=params.get("summary") or "",
            actor=handle, actor_kind="human", roles=roles_for(handle, site, state_dir=ctx.state_dir),
            boundary=params.get("boundary") or None, from_seq=params.get("from_seq"),
            to_seq=params.get("to_seq"),
        )  # fmt: skip
    except AttestRefused as exc:
        return fail(str(exc))
    covers = record["content"]["seal"]["covers"]
    return SkillResult(ok=True, value={"record": record},
                       actions_taken=[f"sealed #{covers['from_seq']} to #{covers['to_seq']} as #{record['seq']}"])  # fmt: skip


def seal(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Close out a period of a log: signed at the prompt for a signed log, an act for a plain one."""
    if _is_plain(params.get("logbook")):
        site, err = resolve_site(params)
        return fail(err) if err else _seal_as_person(params, site, ctx)
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
