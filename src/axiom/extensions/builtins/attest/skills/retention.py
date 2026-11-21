# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A log's retention, enforced with tombstones (unit LD-6, ADR-251 D7).

- ``attest.retention``: declared, in force and next due, per log (a door, by the one check).
- ``attest.tombstone_preview`` (CLI): the dry run of a tombstone of one archived period: every
  record, digest, blob and package it would remove, and nothing removed. For a person who may
  sign tombstones.
- ``attest.tombstone_propose`` (MCP, agent tool): software proposes a tombstone for a person who
  may sign it; it signs nothing.
- ``attest.retention_commit`` (CLI): the erasure's switch committed: reads hide the content.
- ``attest.retention_erase`` (CLI): the retire, a named approver's act: the content goes.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import proposals, registry, retention
from ..logbooks import LogbookError
from ..service import AttestRefused
from ._common import fail, resolve_site


def _who(ctx: Any) -> str | None:
    handle = getattr(getattr(ctx, "principal", None), "handle", None)
    return str(handle) if handle else None


def report(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    if not params.get("logbook"):
        return fail("logbook is required")
    try:
        return SkillResult(ok=True, value=retention.report(site, params["logbook"]))
    except (AttestRefused, LogbookError) as exc:
        return fail(str(exc))


def _may(handle: str | None, site: str, logbook: str, ctx: Any) -> str | None:
    if not handle:
        return "no person to act as"
    try:
        book = registry.get(logbook)
    except LogbookError as exc:
        return str(exc)
    return retention.may_sign(handle, site, book, state_dir=getattr(ctx, "state_dir", None))


def preview(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    logbook = params.get("logbook")
    if not logbook:
        return fail("logbook is required")
    why = _may(_who(ctx), site, logbook, ctx)
    if why:
        return fail(why)
    try:
        dry = retention.preview(site, logbook, archive_id=params.get("archive_id") or None)
    except AttestRefused as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value=dry)


def propose(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Propose a tombstone for a person who may sign it; software never signs one."""
    if "origin" in params:
        return fail("origin is not a parameter: it comes from who is calling")
    try:
        origin = proposals.origin_from_context(ctx, integration=params.get("integration"))
    except AttestRefused as exc:
        return fail(str(exc))
    site, err = resolve_site(params)
    if err:
        return fail(err)
    for key in ("logbook", "archive_id", "for_principal"):
        if not params.get(key):
            return fail(f"{key} is required")
    try:
        d = proposals.propose_tombstone(site_id=site, logbook=params["logbook"], archive_id=params["archive_id"],
                                        origin=origin, for_principal=params["for_principal"],
                                        state_dir=ctx.state_dir)  # fmt: skip
    except (AttestRefused, ValueError) as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value=d, actions_taken=[
        f"proposed a tombstone of {d['logbook']} for {d['for_principal']} as {origin}; nothing is removed "
        "until they sign it and a named approver retires its switch"])


def commit(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    who, tid = _who(ctx), params.get("tombstone_id")
    if not tid:
        return fail("tombstone_id is required")
    try:
        t = retention.get(tid)
        why = _may(who, t["site_id"], t["logbook"], ctx)
        if why:
            return fail(why)
        done = retention.commit(tid, owner=who)
    except AttestRefused as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - the runner's refusal (no gate, busy) says why
        return fail(f"{type(exc).__name__}: {exc}")
    return SkillResult(ok=True, value=done, actions_taken=[
        f"committed tombstone {tid}: reads of #{done['from_seq']} to #{done['to_seq']} show no content; "
        "nothing is removed until a named approver runs attest.retention_erase"])


def erase(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    who, tid = _who(ctx), params.get("tombstone_id")
    if not who:
        return fail("no person to approve as; removing content is a person's explicit act")
    if not tid:
        return fail("tombstone_id is required")
    try:
        done = retention.erase(tid, approver=who, override_soak=params.get("override_soak") or None)
    except AttestRefused as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - the runner's refusal says why
        return fail(f"{type(exc).__name__}: {exc}")
    return SkillResult(ok=True, value=done, actions_taken=[
        f"removed the content of #{done['from_seq']} to #{done['to_seq']} under tombstone {tid}"])


__all__ = ["commit", "erase", "preview", "propose", "report"]
