# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.propose``, ``attest.supplement_propose`` and ``attest.drafts``: software proposes, a
person reads their tray.

These are the only attest skills that write on an agent surface (``mcp``, ``agent_tool``), and
what they write is a draft, never a record. The origin comes from the caller's context, never a
parameter, and nothing here signs or answers for a person: a person completes a proposal with
``attest sign DRAFT_ID`` or on the confirm card (ADR-142, ADR-144).
"""

from __future__ import annotations

import json
from typing import Any

from axiom.infra.skills import SkillResult

from .. import proposals, service
from ..service import AttestRefused
from ._common import fail, parse_fields, resolve_site


def _fields(raw: Any) -> dict[str, Any]:
    """A mapping from an agent, ``["k=v", ...]`` from the CLI, or a JSON object as text."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("fields is a mapping of field id to value")
        return value
    return parse_fields(list(raw))


def propose(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Put a draft in a person's tray, for them to complete and sign."""
    if "origin" in params:
        return fail("origin is not a parameter: it comes from who is calling")
    try:
        origin = proposals.origin_from_context(ctx, integration=params.get("integration"))
    except AttestRefused as exc:
        return fail(str(exc))
    site, err = resolve_site(params)
    if err:
        return fail(err)
    for key in ("logbook", "type", "meaning", "title", "for_principal"):
        if not params.get(key):
            return fail(f"{key} is required")
    try:
        content: dict[str, Any] = {
            "title": params["title"],
            "fields": _fields(params.get("fields")),
        }
        if params.get("body"):
            content["body"] = params["body"]
        d = proposals.propose(
            site_id=site,
            logbook=params["logbook"],
            entry_type=params["type"],
            meaning=params["meaning"],
            content=content,
            origin=origin,
            for_principal=params["for_principal"],
            state_dir=ctx.state_dir,
        )
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return SkillResult(
        ok=True,
        value=d,
        actions_taken=[
            f"proposed {d['logbook']}.{d['entry_type']} for {d['for_principal']} "
            f"as {origin}; it waits in their log tray to be signed"
        ],
    )


def drafts(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The caller's own open drafts, oldest first, filterable by origin prefix."""
    handle = getattr(getattr(ctx, "principal", None), "handle", None)
    if not handle:
        return fail("no person to read a tray for")
    rows = service.list_drafts(
        for_principal=handle,
        site_id=params.get("site") or None,
        logbook=params.get("logbook") or None,
        origin_prefix=params.get("origin") or None,
        limit=int(params.get("limit") or 50),
    )
    return SkillResult(ok=True, value={"for_principal": handle, "drafts": rows})


def supplement_propose(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Propose a supplement to a signed entry, for the person who signed it unless another is
    named. It is a draft: a person completes and signs it (unit L2)."""
    if "origin" in params:
        return fail("origin is not a parameter: it comes from who is calling")
    try:
        origin = proposals.origin_from_context(ctx, integration=params.get("integration"))
    except AttestRefused as exc:
        return fail(str(exc))
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
        content: dict[str, Any] = {"fields": _fields(params.get("fields"))}
        for key in ("title", "body"):
            if params.get(key):
                content[key] = params[key]
        d = proposals.propose_supplement(
            site_id=site,
            target=target,
            reason=params.get("reason"),
            content=content,
            origin=origin,
            for_principal=params.get("for_principal") or None,
            state_dir=ctx.state_dir,
        )
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return SkillResult(
        ok=True,
        value=d,
        actions_taken=[
            f"proposed a {d['reason']} to {d['logbook']} record {target} for "
            f"{d['for_principal']} as {origin}; it waits in their log tray to be signed"
        ],
    )
