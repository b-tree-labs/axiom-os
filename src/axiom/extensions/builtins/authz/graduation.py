# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The graduation writer: human resolutions of a proposal → ``authz.graduation``.

Per prd-axiom-authz §5.3. :class:`~.policy.GraduationSource` reads the
``graduation`` table; this module is what writes it. A row is keyed on
(actor, full intent, resource pattern) — the full ``intent.value``, never the
primitive, so approvals of one verb never graduate another verb.

- :func:`record_approval` — a human approved a ``propose_to_human`` verdict
  unchanged: +1 on every row that covers it (a new row for the exact resource
  when none does). A caller that edited the action before letting it run does
  not call this; an edited approval is not a clean one.
- :func:`record_denial` — a human denied the proposal: every covering row goes
  to ``approvals = 0``, ``graduated = False``.
- :func:`record_revert` — an action that ran was undone (a rollback, a reverted
  change): the same reset as a denial. The action is named by its ``permit``
  receipt, or by the approval that let it run.

``graduated = approvals >= threshold`` is set again on every write. Each
resolution is itself a receipt in ``authz.verdicts`` (``provenance_parent`` is
the resolved receipt), so the table is a projection of the receipts and
:func:`rebuild` can replay it. Those receipts carry the decision
:data:`RESOLUTION`: ``decide()`` made no decision here, so they are never a
``permit``, and nothing that counts permits counts them.

Three properties decide whether a count can be trusted:

- **One receipt, one resolution.** ``graduation_resolutions`` has the resolved
  receipt as its primary key, so the database refuses a replay, including one
  that races the first resolution.
- **A count is read under its row lock.** ``approvals + 1`` from an unlocked
  read could overwrite a concurrent denial's reset and graduate. Writers lock
  every row for the (actor, intent) in id order, then read fresh, and stamp the
  receipt under the lock, so a replay runs in the order the counts were taken.
  :func:`rebuild` shuts writers out, so it cannot miss one that is committing.
- **Who may raise trust is checked; who may lower it is not.** An approval's
  ``by`` must be a well-formed handle, not the actor, and not a known machine.
  Handles carry no kind, so this cannot prove that ``by`` is a person: the
  caller must have authenticated one. A denial or revert only ever sends the
  actor back to a person, so a machine may report one.
"""

from __future__ import annotations

import functools
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from axiom.extensions.builtins.authz.db_models import Graduation, GraduationResolution, Verdict
from axiom.governance import ResourcePattern, ResourceRef
from axiom.vega.identity.principal import parse_handle

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: Default approvals before a pair graduates (prd-axiom-authz §5.3; matches
#: ``DecideContext.default_threshold``). A row keeps the threshold it was created with.
DEFAULT_THRESHOLD = 5

#: ``Verdict.decision`` on a resolution receipt. Not a ``Decision``: a person
#: resolved something, and ``decide()`` decided nothing.
RESOLUTION = "graduation_resolution"

#: Receipt reasons that mark a resolution (kept stable: rebuild() replays on them).
APPROVED = "graduation: approved"
DENIED = "graduation: denied"
REVERTED = "graduation: reverted"
_COUNTS_UP = {APPROVED}

#: Handles the platform itself acts under, which no agent roster lists: the
#: ``Action.approve()`` default (``@gate:auto``, ADR-142) and the surfaces that
#: mint capabilities.
_PLATFORM_NAMES = frozenset({"authority", "config", "gate", "system"})


class ResolutionRefused(ValueError):
    """This resolution may not be recorded. Nothing was written."""


class AlreadyResolved(ResolutionRefused):
    """This receipt has been resolved. One proposal counts at most once."""


@functools.lru_cache(maxsize=1)
def _roster_names() -> frozenset[str]:
    from axiom.agents import roster

    return frozenset(record.handle.lower() for record in roster.load_roster())


def _agent_names() -> frozenset[str]:
    """Every agent this install knows, installed extensions included."""
    try:
        return _roster_names()  # a failure is not cached, so it is asked again
    except Exception:  # noqa: BLE001 — the caller treats an empty roster as a refusal
        return frozenset()


def _require_resolver(by: str) -> str:
    """The name in ``by``, which must be a well-formed principal handle."""
    try:
        return parse_handle(by)[0].lower()
    except ValueError as exc:
        raise ResolutionRefused(f"resolver is not a principal handle: {exc}") from exc


def _require_approver(by: str, actor: str) -> None:
    """``by`` may raise ``actor``'s trust, as far as a handle can show.

    The name is compared without its context, because ``@name`` and
    ``@name:context`` are one principal in its home context. The roster is what
    tells a platform agent from a person, so a roster that can't be read refuses
    every approval rather than letting an agent through.
    """
    name = _require_resolver(by)
    if name == parse_handle(actor)[0].lower():
        raise ResolutionRefused(f"{by} cannot approve its own proposal")
    agents = _agent_names()
    if not agents:
        raise ResolutionRefused("cannot tell an agent from a person: no agent roster")
    if name in agents or name in _PLATFORM_NAMES:
        raise ResolutionRefused(f"{by} is a platform agent, and an approval needs a person")


def matching_rows(
    session: Session, *, actor: str, intent: str, resource: str, for_update: bool = False
) -> list[Graduation]:
    """The graduation rows covering (actor, intent, resource), by ``ResourcePattern``.

    ``for_update`` locks every row for the (actor, intent), in id order so two
    writers cannot deadlock, and re-reads them, so the count is the committed one
    and not a copy this session already held.
    """
    ref = ResourceRef.parse(resource)
    stmt = (
        select(Graduation)
        .where(Graduation.actor == actor)
        .where(Graduation.intent_class == intent)
    )
    if for_update:
        stmt = (
            stmt.order_by(Graduation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    rows = session.execute(stmt).scalars()
    return [r for r in rows if ResourcePattern(r.resource_pattern).matches(ref)]


def record_approval(
    session: Session, proposal: Verdict, *, by: str, threshold: int = DEFAULT_THRESHOLD
) -> None:
    """A human approved ``proposal`` (a ``propose_to_human`` receipt) unchanged.

    Raises :class:`ResolutionRefused` if ``by`` may not approve it, and
    :class:`AlreadyResolved` if it has been resolved.
    """
    _require_approver(by, proposal.actor)
    _require_kind(proposal, APPROVED, proposal.decision == "propose_to_human")
    _record(session, proposal, APPROVED, by=by, threshold=threshold)


def record_denial(
    session: Session, proposal: Verdict, *, by: str, threshold: int = DEFAULT_THRESHOLD
) -> None:
    """A human denied ``proposal`` (a ``propose_to_human`` receipt)."""
    _require_resolver(by)
    _require_kind(proposal, DENIED, proposal.decision == "propose_to_human")
    _record(session, proposal, DENIED, by=by, threshold=threshold)


def record_revert(
    session: Session, taken: Verdict, *, by: str, threshold: int = DEFAULT_THRESHOLD
) -> None:
    """The action ``taken`` was undone after it ran.

    ``taken`` is the action's ``permit`` receipt or, when a human approved it, the
    resolution that did, because an approved action has no ``permit`` of its own.
    """
    _require_resolver(by)
    approved = taken.decision == RESOLUTION and taken.reason.startswith(f"{APPROVED} by ")
    _require_kind(taken, REVERTED, taken.decision == "permit" or approved)
    _record(session, taken, REVERTED, by=by, threshold=threshold)


def rebuild(session: Session) -> None:
    """Recompute the rows from the resolution receipts, oldest first.

    A row no receipt touches stays as it stands, so an imported or hand-set row
    survives. A row a receipt touches is recomputed from zero and keeps its
    threshold, so an imported row's starting count is lost once a resolution
    covers it, which sends that pair back to a person and never the other way. A
    row that has gone comes back at the threshold its receipts were counted
    against.
    """
    # A writer inserts here before it touches a row, so once this is held every
    # resolution is committed and in the list below. SQLite has one writer anyway.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE graduation_resolutions IN EXCLUSIVE MODE"))
    resolutions = session.execute(
        select(Verdict, GraduationResolution.threshold)
        .join(GraduationResolution, GraduationResolution.resolution_id == Verdict.id)
        .where(Verdict.decision == RESOLUTION)
        .order_by(Verdict.decided_at, Verdict.id)
    ).all()
    recomputed: set[str] = set()
    for receipt, threshold in resolutions:
        outcome = receipt.reason.split(" by ", 1)[0]
        if outcome not in (APPROVED, DENIED, REVERTED):
            continue
        rows = _covering_rows(session, receipt, outcome, threshold=threshold)
        for row in rows:
            if row.id not in recomputed:
                recomputed.add(row.id)
                row.approvals = 0
        _count(rows, outcome, receipt.decided_at)
    session.commit()


def _require_kind(resolved: Verdict, outcome: str, fits: bool) -> None:
    if not fits:
        raise ResolutionRefused(f"{outcome} cannot resolve {resolved.id} ({resolved.decision!r})")


def _record(
    session: Session, resolved: Verdict, outcome: str, *, by: str, threshold: int
) -> None:
    resolved_id = resolved.id
    resolution_id = f"authz-{uuid.uuid4().hex}"
    # First, so that a replay stops before anything else is written, and a racing
    # one waits here for the first to commit.
    session.add(GraduationResolution(
        resolved_receipt_id=resolved_id, resolution_id=resolution_id, threshold=threshold,
    ))
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise AlreadyResolved(f"{resolved_id} has already been resolved") from exc
    rows = _covering_rows(session, resolved, outcome, threshold=threshold)
    at = datetime.now(UTC)
    session.add(Verdict(
        id=resolution_id,
        decided_at=at,
        actor=resolved.actor,
        intent=resolved.intent,
        resource=resolved.resource,
        classification=resolved.classification,
        capability_id=resolved.capability_id,
        context_fragment_id=resolved.context_fragment_id,
        provenance_parent=resolved_id,
        federation_origin=resolved.federation_origin,
        dedup_key=resolved.dedup_key,
        decision=RESOLUTION,
        reason=f"{outcome} by {by}",
        matched_rules=None,
    ))
    _count(rows, outcome, at)
    session.commit()


def _covering_rows(
    session: Session, receipt: Verdict, outcome: str, *, threshold: int
) -> list[Graduation]:
    """The locked rows this resolution applies to, creating one for a first approval."""
    key = {"actor": receipt.actor, "intent": receipt.intent, "resource": receipt.resource}
    rows = matching_rows(session, for_update=True, **key)
    if rows or outcome not in _COUNTS_UP:
        return rows
    row = Graduation(
        id=f"grad-{uuid.uuid4().hex}",
        actor=receipt.actor,
        intent_class=receipt.intent,
        resource_pattern=receipt.resource,
        approvals=0,
        threshold=threshold,
        graduated=False,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # A concurrent first approval got there first. Count on its row.
        return matching_rows(session, for_update=True, **key)
    return [row]


def _count(rows: list[Graduation], outcome: str, at: datetime) -> None:
    for row in rows:
        row.approvals = row.approvals + 1 if outcome in _COUNTS_UP else 0
        row.graduated = row.approvals >= row.threshold
        row.last_update = at


__all__ = [
    "APPROVED",
    "AlreadyResolved",
    "DEFAULT_THRESHOLD",
    "DENIED",
    "GraduationResolution",
    "REVERTED",
    "RESOLUTION",
    "ResolutionRefused",
    "matching_rows",
    "rebuild",
    "record_approval",
    "record_denial",
    "record_revert",
]
