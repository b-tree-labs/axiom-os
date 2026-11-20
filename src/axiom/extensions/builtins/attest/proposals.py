# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Software proposes a draft for a person (logbook unit L1; ADR-142 rule 1, ADR-144).

Every rule about proposing lives here: which origins exist, where a caller's origin comes
from, who may receive a proposal, and how many may wait. The rule that software never supplies
an observed value stays in :func:`service.create_draft`, the one place it lives. Nothing here
signs, answers for a person or imports anything that does
(``tests/logbook_acceptance/test_agent_rule.py``).

See ``docs/working/design-note-l1-proposals.md``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from . import registry, service, store
from .db_models import AttestDraft
from .logbooks import PLATFORM_ADMIN_ROLES, LogbookError
from .roles import roles_for
from .service import AttestRefused

__all__ = [
    "MAX_OPEN_PER_ORIGIN",
    "check_recipient",
    "origin_for_key",
    "origin_from_context",
    "propose",
    "propose_seal",
    "propose_supplement",
    "validate_origin",
]

#: Open drafts one origin may have waiting for one person.
MAX_OPEN_PER_ORIGIN = 25

#: ``agent:<id>``, ``integration:<ext>`` or ``rule:<id>``; never ``human``.
_ORIGIN = re.compile(r"^(agent|integration|rule):([a-z0-9][a-z0-9_.\-]*)$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.\-]*$")

#: Surfaces whose caller is software when the dispatcher stamped no actor.
_SOFTWARE_SURFACES = ("mcp", "chat")


def _agent_names() -> set[str]:
    """Handles on the agent roster. A seam, so tests name their own."""
    from axiom.agents.roster import load_roster

    try:
        return {a.handle for a in load_roster()}
    except Exception:  # noqa: BLE001 - an unreadable roster names no agent
        return set()


def _installed_extensions() -> set[str]:
    """Names of the extensions installed on this node. A seam, so tests name their own."""
    from axiom.extensions.discovery import discover_extensions

    try:
        return {e.name for e in discover_extensions()}
    except Exception:  # noqa: BLE001 - nothing discovered means no integration
        return set()


def validate_origin(origin: str) -> str:
    """Return ``origin`` if it names software, else refuse. ``human`` is never a proposal."""
    if not isinstance(origin, str) or not _ORIGIN.match(origin):
        raise AttestRefused(
            f"origin {origin!r} is not a proposer: use agent:<id>, integration:<ext> or "
            "rule:<id>; a person writes their own draft, software only proposes"
        )
    return origin


def _name_of(handle: str) -> str:
    from axiom.vega.identity.principal import parse_handle

    name, _context = parse_handle(handle)
    return name


def origin_from_context(ctx: Any, *, integration: str | None = None) -> str:
    """The origin of a skill call, from what the dispatcher stamped on ``ctx``.

    ``ctx.actor`` (``@name:context``) gives ``agent:<name>``. With no actor, a call over MCP or
    chat is software and is named by its surface. A CLI call with no actor is a person at a
    terminal, refused unless ``integration`` names an installed extension (CLI only)."""
    surface = getattr(ctx, "surface", None)
    if integration:
        if surface != "cli":
            raise AttestRefused("--integration is named at the command line only")
        if integration not in _installed_extensions():
            raise AttestRefused(f"{integration!r} is not an extension installed on this node")
        return validate_origin(f"integration:{integration}")
    actor = getattr(ctx, "actor", None)
    if actor:
        try:
            name = _name_of(actor)
        except ValueError as exc:
            raise AttestRefused(f"the caller {actor!r} is not a handle: {exc}") from None
        return validate_origin(f"agent:{name.lower()}")
    if surface in _SOFTWARE_SURFACES:
        return validate_origin(f"agent:{surface}")
    raise AttestRefused(
        "a proposal comes from software: name the installed extension proposing it with "
        "--integration EXT, or write your own entry with `log <log> new`"
    )


def origin_for_key(principal: str) -> str:
    """The origin of a Bearer key: an installed extension or a roster agent, never a person."""
    try:
        name = _name_of(principal)
    except ValueError as exc:
        raise AttestRefused(f"the key's principal {principal!r} is not a handle: {exc}") from None
    if name in _installed_extensions():
        return validate_origin(f"integration:{name}")
    if name in _agent_names():
        return validate_origin(f"agent:{name}")
    raise AttestRefused(
        f"{principal} is neither an installed extension nor an agent; a person writes a draft "
        "in their own session, with POST /drafts"
    )


def check_recipient(
    site_id: str, logbook: str, entry_type: str, for_principal: str, state_dir: Path
) -> None:
    """Refuse a recipient who is not a person able to sign ``logbook.entry_type`` at the site."""
    from axiom.vega.identity.principal import parse_handle

    try:
        name, context = parse_handle(for_principal or "")
    except ValueError as exc:
        raise AttestRefused(f"{for_principal!r} is not a person's handle: {exc}") from None
    if not context:
        raise AttestRefused(
            f"{for_principal!r}: a recipient's handle names its context, @name:context"
        )
    if name in _agent_names():
        raise AttestRefused(f"{for_principal} is an agent; a proposal is for a person to sign")
    try:
        et = registry.get(logbook).type(entry_type)
    except LogbookError as exc:
        raise AttestRefused(str(exc)) from None
    held = set(roles_for(for_principal, site_id, state_dir=state_dir)) - set(PLATFORM_ADMIN_ROLES)
    if not held & set(et.roles):
        raise AttestRefused(
            f"{for_principal} holds no role at {site_id} that may sign {logbook}.{entry_type} "
            f"(needs one of {list(et.roles)}); an administrator grants one first"
        )


def _open_count(origin: str, for_principal: str) -> int:
    with store.session_scope() as s:
        return s.execute(
            select(func.count())
            .select_from(AttestDraft)
            .where(
                AttestDraft.origin == origin,
                AttestDraft.for_principal == for_principal,
                AttestDraft.status == "open",
            )
        ).scalar_one()


def _publish_created(d: dict[str, Any]) -> None:
    """``attest.<logbook>.draft_created``, after the commit, best effort (as ``signed`` is)."""
    try:
        from axiom.infra.bus import get_default_eventbus

        get_default_eventbus().publish(
            f"attest.{d['logbook']}.draft_created",
            {
                "schema_version": 1,
                "draft_id": d["draft_id"],
                "site_id": d["site_id"],
                "logbook": d["logbook"],
                "entry_type": d["entry_type"],
                "origin": d["origin"],
                "for_principal": d["for_principal"],
            },
            source="attest",
        )
    except Exception:  # noqa: BLE001 - the draft is committed; the tray reads the table
        pass


def propose(
    *,
    site_id: str,
    logbook: str,
    entry_type: str,
    meaning: str,
    content: dict[str, Any],
    origin: str,
    for_principal: str,
    state_dir: Path,
) -> dict[str, Any]:
    """Put a draft in ``for_principal``'s tray. Returns the draft as :func:`service.draft` does."""
    validate_origin(origin)
    check_recipient(site_id, logbook, entry_type, for_principal, state_dir)
    if _open_count(origin, for_principal) >= MAX_OPEN_PER_ORIGIN:
        raise AttestRefused(
            f"{origin} already has {MAX_OPEN_PER_ORIGIN} open proposals for {for_principal}; "
            "they must be signed or discarded before it proposes another"
        )
    draft_id = service.create_draft(
        site_id=site_id,
        logbook=logbook,
        entry_type=entry_type,
        meaning=meaning,
        content=content,
        origin=origin,
        for_principal=for_principal,
    )
    d = service.draft(draft_id)
    _publish_created(d)
    return d


def propose_supplement(
    *,
    site_id: str,
    target: str,
    reason: str | None,
    content: dict[str, Any],
    origin: str,
    for_principal: str | None,
    state_dir: Path,
) -> dict[str, Any]:
    """Put a supplement to ``target`` in a person's tray: by default the person who signed
    ``target`` (unit L2). The same recipient checks and cap as :func:`propose`; the rules about
    the target, the reason and observed values are the service's."""
    validate_origin(origin)
    rec = service.record(target) if isinstance(target, str) and target else None
    if rec is None or rec["site_id"] != site_id:
        raise AttestRefused(f"no record {target} at {site_id}")
    recipient = for_principal or rec["signer"]["principal"]
    check_recipient(site_id, rec["logbook"], service.root_entry_type(target), recipient, state_dir)
    if _open_count(origin, recipient) >= MAX_OPEN_PER_ORIGIN:
        raise AttestRefused(
            f"{origin} already has {MAX_OPEN_PER_ORIGIN} open proposals for {recipient}; "
            "they must be signed or discarded before it proposes another"
        )
    draft_id = service.create_supplement_draft(
        site_id=site_id,
        target=target,
        reason=reason,
        content=content,
        origin=origin,
        for_principal=recipient,
    )
    d = service.draft(draft_id)
    _publish_created(d)
    return d


def propose_seal(
    *,
    site_id: str,
    logbook: str,
    summary: str,
    origin: str,
    for_principal: str,
    state_dir: Path,
    boundary: str | None = None,
    from_seq: int | None = None,
    to_seq: int | None = None,
) -> dict[str, Any]:
    """Put a close-out draft with a proposed summary in a person's tray (unit L5). The person must
    hold one of the log's ``[seal] by_roles``; the range, chain head and open items are the
    server's and no parameter of this call touches them."""
    from axiom.vega.identity.principal import parse_handle

    validate_origin(origin)
    try:
        book = registry.get(logbook)
    except LogbookError as exc:
        raise AttestRefused(str(exc)) from None
    if book.seal is None:
        raise AttestRefused(f"{logbook} declares no seal, so it has no close-out")
    try:
        name, context = parse_handle(for_principal or "")
    except ValueError as exc:
        raise AttestRefused(f"{for_principal!r} is not a person's handle: {exc}") from None
    if not context:
        raise AttestRefused(f"{for_principal!r}: a recipient's handle names its context, @name:context")
    if name in _agent_names():
        raise AttestRefused(f"{for_principal} is an agent; a proposal is for a person to sign")
    held = set(roles_for(for_principal, site_id, state_dir=state_dir)) - set(PLATFORM_ADMIN_ROLES)
    if not held & set(book.seal.by_roles):
        raise AttestRefused(
            f"{for_principal} holds no role at {site_id} that may seal {logbook} "
            f"(needs one of {list(book.seal.by_roles)})"
        )
    if _open_count(origin, for_principal) >= MAX_OPEN_PER_ORIGIN:
        raise AttestRefused(
            f"{origin} already has {MAX_OPEN_PER_ORIGIN} open proposals for {for_principal}; "
            "they must be signed or discarded before it proposes another"
        )
    draft_id = service.create_seal_draft(
        site_id=site_id, logbook=logbook, summary=summary, origin=origin,
        for_principal=for_principal, boundary=boundary, from_seq=from_seq, to_seq=to_seq,
    )  # fmt: skip
    d = service.draft(draft_id)
    _publish_created(d)
    return d
