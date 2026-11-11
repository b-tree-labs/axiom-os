# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.new`` and ``attest.sign``: a person signs at the CLI.

Both end the same way: the presentation is printed, the person answers
``sign``, ``hold`` or ``ask`` at the prompt, and only ``sign`` chains a record.
There is no flag that answers for them, and with no one at the prompt nothing
is signed. These skills are exposed on the CLI only, never over MCP or as an
agent tool, because software drafts and never signs (ADR-142).
"""

from __future__ import annotations

import time
from typing import Any

from axiom.infra.skills import SkillResult
from axiom.vega.identity.node_key import NodeKeyUnavailable

from .. import service, signing
from ..roles import roles_for
from ..service import AttestRefused, Signatory
from ._common import fail, parse_fields, resolve_site


def _respond(draft_id: str, site: str, ctx: Any) -> SkillResult:
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was signed")
    handle = ctx.principal.handle
    who = Signatory(
        principal=ctx.principal,
        roles=roles_for(handle, site, state_dir=ctx.state_dir),
        display=handle,
    )
    try:
        pres = service.present(draft_id, modality="cli")
        started = time.monotonic()
        answer = (
            ctx.user_prompt(f"{pres.rendered}\n\ndigest {pres.digest}\nsign, hold or ask? ")
            .strip()
            .lower()
        )
        service.respond(
            pres.presentation_id,
            principal=ctx.principal,
            answer=answer,
            via="cli",
            transcript=answer,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if answer != "sign":
            return SkillResult(ok=True, value={"status": answer, "draft_id": draft_id})
        result = service.sign(pres.presentation_id, signatory=who, signer=signing.signer())
    except (AttestRefused, NodeKeyUnavailable) as exc:
        return fail(f"not signed: {exc}")
    rec = result.record
    return SkillResult(
        ok=True,
        value={
            "status": "signed",
            "attestation_id": rec["attestation_id"],
            "site_id": rec["site_id"],
            "logbook": rec["logbook"],
            "seq": rec["seq"],
            "digest": rec["digest"],
            "uri": f"axiom://attest/sha256:{rec['digest']}",
        },
        actions_taken=[f"signed {rec['logbook']} #{rec['seq']} at {rec['site_id']}"],
    )


def new(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Write and sign an entry in one step."""
    if ctx is None or ctx.user_prompt is None:
        return fail("signing needs a person at the prompt; nothing was written")
    site, err = resolve_site(params)
    if err:
        return fail(err)
    for key in ("logbook", "type", "meaning", "title"):
        if not params.get(key):
            return fail(f"{key} is required")
    try:
        fields = parse_fields(params.get("fields"))
        content = {"title": params["title"], "fields": fields}
        if params.get("body"):
            content["body"] = params["body"]
        draft_id = service.create_draft(
            site_id=site,
            logbook=params["logbook"],
            entry_type=params["type"],
            meaning=params["meaning"],
            content=content,
            origin="human",
            for_principal=ctx.principal.handle,
        )
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)


def sign(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Complete and sign a draft someone or something proposed for you."""
    draft_id = params.get("draft_id")
    if not draft_id:
        return fail("draft_id is required")
    try:
        d = service.draft(draft_id)
        site, err = resolve_site({"site": d["site_id"]})
        if err:
            return fail(err)
        values = parse_fields(params.get("fields"))
        if values:
            who = Signatory(principal=ctx.principal, roles=(), display=ctx.principal.handle)
            service.fill(draft_id, values, by=who)
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return _respond(draft_id, site, ctx)
