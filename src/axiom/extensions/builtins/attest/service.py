# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Draft, present, sign (ADR-142..144).

Software drafts; a person signs. The steps are separate on purpose:

1. :func:`create_draft` records what was proposed and by whom. Software may
   propose, but it may never supply a value the logbook says the person must
   observe.
2. :func:`fill` lets the person the draft is for enter or change values. Each
   field keeps who supplied it.
3. :func:`present` freezes exactly what the person is shown, through which
   modality, rendered by which presenter, and its digest. It expires.
4. :func:`respond` records the person's answer: ``sign``, ``hold`` or ``ask``
   (R21). Every answer is evidence; ``ask`` may carry a correction, which
   yields a fresh presentation.
5. :func:`sign` chains a record for a presentation the person answered
   ``sign``. It refuses unless the content is still what was presented, the
   person is who the draft is for, every modality the logbook requires has
   presented it, their posture meets the type's floor, and they hold one of
   the type's roles. Administration roles never count (ADR-142 rule 8).

Records are chained per (site, logbook) under a row lock on the chain head, so
concurrent signers get a gapless sequence. If the node cannot sign, nothing is
written and the draft stays open.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from axiom.attest.anchor import merkle_root, sign_anchor, verify_anchor
from axiom.attest.canonical import CanonicalError, digest, normalise
from axiom.attest.chain import GENESIS, Signer, VerifyReport, seal, verify_chain
from axiom.infra.principal import PrincipalContext

from . import presenters, registry, store
from .db_models import (
    AttestAnchor,
    AttestDraft,
    AttestGrantUse,
    AttestPresentation,
    AttestRecord,
    AttestResponse,
)
from .logbooks import (
    DEVICE_CLASSES,
    PLATFORM_ADMIN_ROLES,
    SIGNING_POSTURES,
    EntryType,
    Logbook,
    LogbookError,
)

RESPONSES = ("sign", "hold", "ask")

#: Why a person supplements a signed entry (unit L2). The words a person reads are the frozen
#: "Correction", "Clarification" and "Addition" (``presenters.REASON_WORDS``).
SUPPLEMENT_REASONS = ("correction", "clarification", "addition")

#: The meaning a supplement signs under, by reason. No meaning is added to the closed vocabulary:
#: a correction says the earlier value was wrong; the other two add to it.
SUPPLEMENT_MEANINGS = {
    "correction": "corrected",
    "clarification": "authored",
    "addition": "authored",
}

#: Record kinds a supplement may point at. Acknowledgements, co-signatures and seals (L3 to L5)
#: are acts on an entry, not entries, so they are not supplemented.
SUPPLEMENTABLE_KINDS = ("statement", "supplement")

#: Record kinds a person may acknowledge (unit L3): the entries, never an act on one.
ACKNOWLEDGEABLE_KINDS = ("statement", "supplement")

#: What a co-signature means (unit L4): the second person puts their authority behind the entry.
COSIGN_MEANING = "approved"


@dataclass(frozen=True)
class SigningRules:
    """What a draft asks of the person and device who sign it, named once per kind (unit L3).

    A statement or supplement takes them from its entry type. An acknowledgement takes the
    union of the log's roles, the log's posture floor, no freshness window, every device class
    (a phone may acknowledge) and no presence rule. ``grants.mint`` and :func:`sign` read these
    and nothing else, so a later kind adds a row to :func:`rules_for` and changes neither."""

    roles: tuple[str, ...]
    posture: str
    fresh_within_seconds: int | None
    sign_devices: tuple[str, ...]
    presence: str | None
    #: what the rules describe, for refusals: ``demo_log.ROUND_CHECK`` or ``demo_log (acknowledge)``
    what: str


def _rules(d: Any, logbook: Logbook, et: EntryType) -> SigningRules:
    kind = _kind(d)
    if kind == "cosign" and et.cosign is not None:
        # A co-signature is the declared second person's: their roles, their assurance, the
        # type's devices minus a phone (a phone acknowledges; it does not countersign) and the
        # type's presence rule.
        return SigningRules(
            roles=et.cosign.roles,
            posture=et.cosign.posture,
            fresh_within_seconds=et.cosign.fresh_within_seconds,
            sign_devices=tuple(c for c in et.sign_devices if c != "phone"),
            presence=et.presence,
            what=f"{d.logbook}.{d.entry_type} (co-sign)",
        )
    if kind == "ack":
        roles = tuple(dict.fromkeys(r for t in logbook.types for r in t.roles))
        return SigningRules(
            roles=roles,
            posture=logbook.posture,
            fresh_within_seconds=None,
            sign_devices=DEVICE_CLASSES,
            presence=None,
            what=f"{logbook.id} (acknowledge)",
        )
    return SigningRules(
        roles=et.roles,
        posture=et.posture,
        fresh_within_seconds=et.fresh_within_seconds,
        sign_devices=et.sign_devices,
        presence=et.presence,
        what=f"{d.logbook}.{d.entry_type}",
    )


def rules_for(draft_id: str) -> SigningRules:
    """The signing rules of one draft, by its kind."""
    with store.session_scope() as s:
        d = _load_draft(s, draft_id)
        _, et = _entry_type(d.logbook, d.entry_type)
        return _rules(d, registry.get(d.logbook), et)


class AttestRefused(ValueError):
    """The request would record something that is not a person's act."""


@dataclass(frozen=True)
class Signatory:
    """The person responding. ``roles`` come from the site's role source,
    never from the person's own say-so."""

    principal: PrincipalContext
    roles: tuple[str, ...]
    display: str = ""


@dataclass(frozen=True)
class Presentation:
    presentation_id: str
    digest: str
    rendered: str  # the card density; what a CLI prints
    statement: dict[str, Any]
    modality: str = "cli"
    presenter: str = ""
    forms: dict[str, str] | None = None
    speakable: str = ""
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Response:
    response_id: str
    presentation_id: str
    answer: str
    via: str
    next_presentation: Presentation | None = None


@dataclass(frozen=True)
class SignResult:
    status: str
    record: dict[str, Any] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _entry_type(logbook_id: str, type_id: str) -> tuple[str, EntryType]:
    try:
        logbook = registry.get(logbook_id)
        return logbook.version, logbook.type(type_id)
    except LogbookError as exc:
        raise AttestRefused(str(exc)) from None


def _check_fields(et: EntryType, fields: dict[str, Any]) -> None:
    known = {f.id for f in et.fields}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise AttestRefused(f"{et.logbook_id}.{et.id}: unknown field {unknown}")


def _readings(et: EntryType, site_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Check and complete every ``readings`` value against its instruments.

    A reading may be entered as ``"950"`` or ``{"value": "950", "unit": "kW"}``.
    It is stored as ``{"value", "unit", "uncertainty"}``: the value exactly as
    entered (a decimal string, never a float), the instrument's unit (a
    different unit is refused, not converted) and the instrument's declared
    uncertainty, or ``{"kind": "unquantified"}``.
    """
    from decimal import Decimal, InvalidOperation

    from . import field_sources

    out = dict(fields)
    for f in et.fields:
        if f.type != "readings" or f.id not in fields:
            continue
        raw = fields[f.id]
        if not isinstance(raw, dict):
            raise AttestRefused(f"{f.id}: readings are given per instrument, as a mapping")
        if not f.from_site:
            raise AttestRefused(f"{et.logbook_id}.{et.id}.{f.id}: readings need a from_site source")
        try:
            known = {i.id: i for i in field_sources.resolve(f.from_site, site_id)}
        except LookupError as exc:
            raise AttestRefused(str(exc)) from None
        clean: dict[str, Any] = {}
        for inst_id, entry in raw.items():
            inst = known.get(inst_id)
            if inst is None:
                raise AttestRefused(
                    f"{f.id}: {inst_id!r} is not an instrument at {site_id} "
                    f"(known: {sorted(known)})"
                )
            value, unit = (
                (entry.get("value"), entry.get("unit"))
                if isinstance(entry, dict)
                else (entry, None)
            )
            if not isinstance(value, str):
                raise AttestRefused(
                    f"{f.id}.{inst_id}: enter the value as a decimal string, as read"
                )
            try:
                if not Decimal(value).is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                raise AttestRefused(
                    f"{f.id}.{inst_id}: {value!r} is not a decimal reading"
                ) from None
            if unit is not None and unit != inst.unit:
                raise AttestRefused(
                    f"{f.id}.{inst_id}: unit {unit!r} is not the instrument's unit {inst.unit!r}"
                )
            clean[inst_id] = {
                "value": value,
                "unit": inst.unit,
                "uncertainty": dict(inst.uncertainty)
                if inst.uncertainty
                else {"kind": "unquantified"},
            }
        out[f.id] = clean
    return out


def _missing_readings(et: EntryType, site_id: str, fields: dict[str, Any]) -> list[str]:
    from . import field_sources

    missing: list[str] = []
    for f in et.fields:
        if f.type == "readings" and f.required and f.from_site:
            have = fields.get(f.id) or {}
            try:
                want = [i.id for i in field_sources.resolve(f.from_site, site_id)]
            except LookupError:
                continue
            missing += [f"{f.id}.{i}" for i in want if i not in have]
    return missing


def _normalised(value: dict[str, Any]) -> dict[str, Any]:
    try:
        return normalise(value)
    except CanonicalError as exc:
        raise AttestRefused(f"content cannot be signed: {exc}") from None


# -- drafts ------------------------------------------------------------------


def create_draft(
    *,
    site_id: str,
    logbook: str,
    entry_type: str,
    meaning: str,
    content: dict[str, Any],
    origin: str,
    for_principal: str,
    kind: str = "statement",
    target: str | None = None,
    reason: str | None = None,
) -> str:
    """Record a proposal for ``for_principal`` to sign. Returns the draft id.

    ``origin`` is ``"human"`` when the person typed it, otherwise who proposed
    it (``agent:<id>``, ``integration:<ext>``, ``rule``).

    ``kind``, ``target`` and ``reason`` make the draft an action on another record (unit L2);
    each kind has its own ``create_<kind>_draft`` that checks the target and calls this. For an
    action, ``entry_type`` is the target entry's type, which decides who may sign it and how,
    and ``meaning`` comes from the action, not from the type's list."""
    version, et = _entry_type(logbook, entry_type)
    if kind == "statement" and meaning not in et.meanings:
        raise AttestRefused(f"{logbook}.{entry_type} cannot carry meaning {meaning!r}")
    if not isinstance(content, dict) or not isinstance(content.get("fields", {}), dict):
        raise AttestRefused("content is {title, body?, fields}")
    fields = dict(content.get("fields") or {})
    _check_fields(et, fields)
    if origin != "human":
        offered = sorted(f.id for f in et.fields if f.observe and f.id in fields)
        if offered:
            raise AttestRefused(
                f"{origin} offered {offered}; the logbook marks them observe, so only "
                "the person may enter them (ADR-144)"
            )
    fields = _readings(et, site_id, fields)
    supplier = for_principal if origin == "human" else origin
    draft_id = str(uuid.uuid4())
    with store.session_scope() as s:
        s.add(
            AttestDraft(
                draft_id=draft_id,
                site_id=site_id,
                logbook=logbook,
                entry_type=entry_type,
                meaning=meaning,
                content=_normalised({**content, "fields": fields}),
                provenance={k: supplier for k in fields},
                origin=origin,
                for_principal=for_principal,
                status="open",
                created_at=_now(),
                kind=kind,
                target=target,
                reason=reason,
            )
        )
        s.commit()
    return draft_id


def _root_type(s: Any, row: AttestRecord) -> str:
    """The entry type of the statement at the root of ``row``'s thread."""
    seen = set()
    while row.kind != "statement":
        if row.target is None or row.attestation_id in seen:
            raise AttestRefused(f"record {row.attestation_id} has no statement at its root")
        seen.add(row.attestation_id)
        row = s.get(AttestRecord, row.target)
        if row is None:
            raise AttestRefused("a record in the thread is missing")
    if not row.entry_type:
        raise AttestRefused(f"record {row.attestation_id} has no entry type")
    return row.entry_type


def _acknowledged_by(s: Any, target: str, principal: str) -> AttestRecord | None:
    return s.execute(
        select(AttestRecord)
        .where(
            AttestRecord.kind == "ack",
            AttestRecord.target == target,
            AttestRecord.signer == principal,
        )
        .limit(1)
    ).scalar_one_or_none()


def create_ack_draft(
    *,
    site_id: str,
    target: str,
    origin: str,
    for_principal: str,
    logbook: str | None = None,
) -> str:
    """A draft by which ``for_principal`` acknowledges the signed record ``target``: "I have seen
    this and it stands" (unit L3). Refuses a target that does not exist at ``site_id`` (or in
    ``logbook`` when named), a target that is not an entry, the signer's own entry, and a second
    acknowledgement by the same person. Who may sign it is :func:`rules_for`'s ``ack`` row."""
    with store.session_scope() as s:
        row = s.get(AttestRecord, target) if isinstance(target, str) and target else None
        if row is None or row.site_id != site_id or (logbook and row.logbook != logbook):
            raise AttestRefused(f"no record {target} in {logbook or 'this log'} at {site_id}")
        if row.kind not in ACKNOWLEDGEABLE_KINDS:
            raise AttestRefused(
                f"record {target} is a {row.kind}; it cannot be acknowledged, only an entry can"
            )
        if row.signer == for_principal:
            raise AttestRefused(
                f"{for_principal} signed record {target}; a signature already stands behind "
                "your own entry, so it is not acknowledged"
            )
        earlier = _acknowledged_by(s, target, for_principal)
        if earlier is not None:
            raise AttestRefused(f"record {target} is already acknowledged by you at #{earlier.seq}")
        entry_type = _root_type(s, row)
        the_logbook = row.logbook
        title = str((row.record.get("content") or {}).get("title") or f"#{row.seq}")
    return create_draft(
        site_id=site_id,
        logbook=the_logbook,
        entry_type=entry_type,
        meaning="acknowledged",
        content={"title": f"Acknowledged: {title}", "fields": {}},
        origin=origin,
        for_principal=for_principal,
        kind="ack",
        target=target,
    )


def _cosigned(s: Any, target: str) -> AttestRecord | None:
    return s.execute(
        select(AttestRecord)
        .where(AttestRecord.kind == "cosign", AttestRecord.target == target)
        .limit(1)
    ).scalar_one_or_none()


def create_cosign_draft(
    *,
    site_id: str,
    target: str,
    origin: str,
    for_principal: str,
    logbook: str | None = None,
) -> str:
    """A draft by which ``for_principal`` co-signs the statement ``target`` (unit L4). Refuses a
    target that does not exist at ``site_id`` (or in ``logbook`` when named), one that is not a
    statement, one whose type declares no ``cosign``, the signer's own entry, and a second
    co-signature. Who may sign it, and how fresh their sign-in must be, is :func:`rules_for`'s
    ``cosign`` row."""
    with store.session_scope() as s:
        row = s.get(AttestRecord, target) if isinstance(target, str) and target else None
        if row is None or row.site_id != site_id or (logbook and row.logbook != logbook):
            raise AttestRefused(f"no record {target} in {logbook or 'this log'} at {site_id}")
        if row.kind != "statement":
            raise AttestRefused(
                f"record {target} is a {row.kind}; it cannot be co-signed, only an entry can"
            )
        _, et = _entry_type(row.logbook, row.entry_type or "")
        if et.cosign is None:
            raise AttestRefused(
                f"{row.logbook}.{et.id} declares no co-signature; record {target} needs none"
            )
        if row.signer == for_principal:
            raise AttestRefused(
                f"{for_principal} signed record {target}; a co-signature is a second person's, "
                "so your own entry is not co-signed by you"
            )
        earlier = _cosigned(s, target)
        if earlier is not None:
            raise AttestRefused(
                f"record {target} is already co-signed by {earlier.signer} at #{earlier.seq}"
            )
        the_logbook = row.logbook
        entry_type = row.entry_type
        title = str((row.record.get("content") or {}).get("title") or f"#{row.seq}")
    return create_draft(
        site_id=site_id,
        logbook=the_logbook,
        entry_type=entry_type,
        meaning=COSIGN_MEANING,
        content={"title": f"Co-signed: {title}", "fields": {}},
        origin=origin,
        for_principal=for_principal,
        kind="cosign",
        target=target,
    )


def _action_content(content: dict[str, Any]) -> dict[str, Any]:
    """An action's content: the title and body when given, and the fields it carries."""
    clean = {k: v for k, v in content.items() if k in ("title", "body") and v}
    clean["fields"] = dict(content.get("fields") or {})
    return clean


def normalised_content(
    site_id: str, logbook: str, entry_type: str, content: dict[str, Any]
) -> dict[str, Any]:
    """``content`` as a supplement draft of ``logbook.entry_type`` at ``site_id`` stores it, so
    a caller can check that a draft says what it means to sign (``axiom.attest.sign``)."""
    _, et = _entry_type(logbook, entry_type)
    clean = _action_content(content if isinstance(content, dict) else {})
    _check_fields(et, clean["fields"])
    return _normalised({**clean, "fields": _readings(et, site_id, clean["fields"])})


def root_entry_type(attestation_id: str) -> str:
    """The entry type that decides who may act on ``attestation_id``: its own, or for an action,
    the type of the statement at the root of its thread."""
    with store.session_scope() as s:
        row = s.get(AttestRecord, attestation_id)
        if row is None:
            raise AttestRefused(f"no record {attestation_id}")
        return _root_type(s, row)


def create_supplement_draft(
    *,
    site_id: str,
    target: str,
    reason: str | None,
    content: dict[str, Any],
    origin: str,
    for_principal: str,
    logbook: str | None = None,
) -> str:
    """A draft that corrects, clarifies or adds to the signed record ``target`` (unit L2).

    Refuses a target that does not exist at ``site_id`` (or in ``logbook`` when named), a target
    that is not an entry, a reason outside :data:`SUPPLEMENT_REASONS`, content with neither a body
    nor a field, a field the target's type does not declare, and an ``observe`` value offered by
    software. The signer is held to the target type's roles and assurance when it is signed."""
    with store.session_scope() as s:
        row = s.get(AttestRecord, target) if isinstance(target, str) and target else None
        if row is None or row.site_id != site_id or (logbook and row.logbook != logbook):
            raise AttestRefused(f"no record {target} in {logbook or 'this log'} at {site_id}")
        if row.kind not in SUPPLEMENTABLE_KINDS:
            raise AttestRefused(
                f"record {target} is a {row.kind}; it cannot be supplemented, only an entry can"
            )
        entry_type = _root_type(s, row)
        the_logbook = row.logbook
    if reason not in SUPPLEMENT_REASONS:
        raise AttestRefused(
            f"a supplement needs a reason, one of {list(SUPPLEMENT_REASONS)}; got {reason!r}"
        )
    if not isinstance(content, dict) or not isinstance(content.get("fields", {}), dict):
        raise AttestRefused("content is {title?, body?, fields}")
    body = content.get("body")
    if not (isinstance(body, str) and body.strip()) and not content.get("fields"):
        raise AttestRefused("a supplement needs a body or at least one field")
    return create_draft(
        site_id=site_id,
        logbook=the_logbook,
        entry_type=entry_type,
        meaning=SUPPLEMENT_MEANINGS[reason],
        content=_action_content(content),
        origin=origin,
        for_principal=for_principal,
        kind="supplement",
        target=target,
        reason=reason,
    )


def _load_draft(s: Any, draft_id: str, *, lock: bool = False) -> AttestDraft:
    q = select(AttestDraft).where(AttestDraft.draft_id == draft_id)
    if lock:
        q = q.with_for_update()
    row = s.execute(q).scalar_one_or_none()
    if row is None:
        raise AttestRefused(f"no draft {draft_id}")
    return row


def draft(draft_id: str) -> dict[str, Any]:
    with store.session_scope() as s:
        return _draft_view(_load_draft(s, draft_id))


def _draft_view(d: AttestDraft) -> dict[str, Any]:
    return {
        "draft_id": d.draft_id,
        "site_id": d.site_id,
        "logbook": d.logbook,
        "entry_type": d.entry_type,
        "meaning": d.meaning,
        "content": d.content,
        "provenance": d.provenance,
        "origin": d.origin,
        "for_principal": d.for_principal,
        "status": d.status,
        "attestation_id": d.attestation_id,
        "kind": d.kind or "statement",
        "target": d.target,
        "reason": d.reason,
    }


def list_drafts(
    *,
    for_principal: str,
    site_id: str | None = None,
    logbook: str | None = None,
    origin_prefix: str | None = None,
    status: str | None = "open",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """One person's drafts, oldest first: the shape of :func:`draft` plus ``created_at`` and
    ``held`` (their latest answer to it was ``hold``, so it stays open and is marked)."""
    q = select(AttestDraft).where(AttestDraft.for_principal == for_principal)
    if site_id:
        q = q.where(AttestDraft.site_id == site_id)
    if logbook:
        q = q.where(AttestDraft.logbook == logbook)
    if origin_prefix:
        q = q.where(AttestDraft.origin.startswith(origin_prefix, autoescape=True))
    if status:
        q = q.where(AttestDraft.status == status)
    q = q.order_by(AttestDraft.created_at, AttestDraft.draft_id).limit(max(1, min(limit, 500)))
    with store.session_scope() as s:
        rows = list(s.execute(q).scalars())
        last: dict[str, str] = {}
        if rows:
            answers = s.execute(
                select(AttestPresentation.draft_id, AttestResponse.answer)
                .join(
                    AttestResponse,
                    AttestResponse.presentation_id == AttestPresentation.presentation_id,
                )
                .where(
                    AttestPresentation.draft_id.in_([d.draft_id for d in rows]),
                    AttestResponse.principal == for_principal,
                )
                .order_by(AttestResponse.at)
            ).all()
            last = {draft_id: answer for draft_id, answer in answers}
        return [
            {
                **_draft_view(d),
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "held": d.status == "open" and last.get(d.draft_id) == "hold",
            }
            for d in rows
        ]


def discard(draft_id: str, *, by: str) -> None:
    """The person a draft is for discards it. Drafts are working state, not evidence, so this
    sets the status and writes no record. Anyone else is told there is no such draft."""
    with store.session_scope() as s:
        d = _load_draft(s, draft_id, lock=True)
        if d.for_principal != by:
            raise AttestRefused(f"no draft {draft_id}")
        if d.status != "open":
            raise AttestRefused(f"draft {draft_id} is already {d.status}")
        d.status = "discarded"
        s.commit()


def fill(draft_id: str, values: dict[str, Any], *, by: Signatory) -> None:
    """The person the draft is for enters or changes field values."""
    handle = by.principal.handle
    with store.session_scope() as s:
        d = _load_draft(s, draft_id, lock=True)
        if d.status != "open":
            raise AttestRefused(f"draft {draft_id} is already {d.status}")
        if handle != d.for_principal:
            raise AttestRefused(f"draft {draft_id} is for {d.for_principal}, not {handle}")
        _, et = _entry_type(d.logbook, d.entry_type)
        _check_fields(et, values)
        values = _readings(et, d.site_id, values)
        content = dict(d.content)
        content["fields"] = {**content.get("fields", {}), **_normalised(values)}
        d.content = content
        d.provenance = {**d.provenance, **{k: handle for k in values}}
        s.commit()


# -- presentation ------------------------------------------------------------


def _kind(d: Any) -> str:
    return getattr(d, "kind", None) or "statement"


def _target_row(s: Any, d: Any) -> AttestRecord:
    row = s.get(AttestRecord, d.target) if d.target else None
    if row is None:
        raise AttestRefused(f"the record draft {d.draft_id} acts on is missing")
    return row


def _statement(d: Any, logbook_version: str, s: Any = None) -> dict[str, Any]:
    """What the person is shown and signs. A statement's keys are exactly those of before L2, so
    its digest is unchanged; an action adds ``target``, ``target_digest`` and ``reason`` and has
    no ``entry_type`` of its own."""
    statement = {
        "site_id": d.site_id,
        "logbook": d.logbook,
        "logbook_version": logbook_version,
        "kind": _kind(d),
        "entry_type": d.entry_type,
        "meaning": d.meaning,
        "content": d.content,
        "origin": d.origin,
        "for_principal": d.for_principal,
        "provenance": d.provenance,
    }
    if _kind(d) != "statement":
        statement["entry_type"] = None
        statement["target"] = d.target
        statement["target_digest"] = _target_row(s, d).digest
        statement["reason"] = d.reason
    return statement


def present(draft_id: str, *, modality: str = "cli") -> Presentation:
    """Freeze what the person will be shown through ``modality``. Refuses an
    incomplete draft, an observed value that did not come from a person, and a
    modality the entry type's confirm policy does not allow."""
    with store.session_scope() as s:
        d = _load_draft(s, draft_id)
        if d.status != "open":
            raise AttestRefused(f"draft {draft_id} is already {d.status}")
        version, et = _entry_type(d.logbook, d.entry_type)
        if modality not in et.confirm.modalities_any:
            raise AttestRefused(
                f"{d.logbook}.{d.entry_type} may not be presented by {modality!r}; "
                f"its confirm policy allows {list(et.confirm.modalities_any)}"
            )
        fields = d.content.get("fields", {})
        if _kind(d) == "statement":
            # An action carries only what it changes, so the type's required fields do not apply.
            missing = [f.id for f in et.fields if f.required and f.id not in fields]
            missing += _missing_readings(et, d.site_id, fields)
            if missing:
                raise AttestRefused(f"required fields not entered: {missing}")
        for f in et.fields:
            if f.observe and f.id in fields and not str(d.provenance.get(f.id, "")).startswith("@"):
                raise AttestRefused(f"observe field {f.id!r} was not entered by a person")
        statement = _statement(d, version, s)
        presenter = presenters.for_type(et, logbook_version=version, site_id=d.site_id)
        if _kind(d) == "supplement":
            presenter = presenters.SupplementPresenter(
                presenter, target=_target_row(s, d).record, reason=d.reason
            )
        elif _kind(d) == "ack":
            presenter = presenters.AckPresenter(presenter, target=_target_row(s, d).record)
        elif _kind(d) == "cosign":
            presenter = presenters.CosignPresenter(presenter, target=_target_row(s, d).record)
        forms = {density: presenter.render(d.content, density) for density in presenters.DENSITIES}
        now = _now()
        pres = Presentation(
            presentation_id=str(uuid.uuid4()),
            digest=digest(statement),
            rendered=forms["card"],
            statement=statement,
            modality=modality,
            presenter=f"{presenter.id}@{presenter.version}",
            forms=forms,
            speakable=presenter.speakable(d.content),
            expires_at=now + timedelta(seconds=et.confirm.timeout_seconds),
        )
        s.add(
            AttestPresentation(
                presentation_id=pres.presentation_id,
                draft_id=draft_id,
                digest=pres.digest,
                statement=statement,
                rendered=pres.rendered,
                modality=modality,
                created_at=now,
                presenter=pres.presenter,
                forms=forms,
                speakable=pres.speakable,
                expires_at=pres.expires_at,
            )
        )
        s.commit()
        return pres


# -- responses ------------------------------------------------------------------


def respond(
    presentation_id: str,
    *,
    principal: PrincipalContext,
    answer: str,
    via: str,
    transcript: str | None = None,
    audio_sha256: str | None = None,
    latency_ms: int | None = None,
    console_id: str | None = None,
    correction: dict[str, Any] | None = None,
) -> Response:
    """Record the person's answer to a presentation (R21: sign, hold or ask).

    Every answer is kept as evidence. ``sign`` makes the presentation signable
    by :func:`sign`. ``hold`` leaves the draft open. ``ask`` with a
    ``correction`` applies it as the person's own entry and returns a new
    presentation of the corrected content; the old one can no longer be signed.
    """
    if answer not in RESPONSES:
        raise AttestRefused(f"response must be one of {RESPONSES}, not {answer!r}")
    handle = principal.handle
    with store.session_scope() as s:
        pres = s.get(AttestPresentation, presentation_id)
        if pres is None:
            raise AttestRefused(f"no presentation {presentation_id}")
        d = _load_draft(s, pres.draft_id, lock=True)
        if d.status != "open":
            raise AttestRefused(f"draft {d.draft_id} is already {d.status}")
        if handle != d.for_principal:
            raise AttestRefused(f"draft {d.draft_id} is for {d.for_principal}, not {handle}")
        version, et = _entry_type(d.logbook, d.entry_type)
        if via not in et.confirm.modalities_any:
            raise AttestRefused(f"{d.logbook}.{d.entry_type} cannot be answered by {via!r}")
        if via == "voice" and answer == "sign" and not et.confirm.voice_confirm_allowed:
            raise AttestRefused(
                f"{d.logbook}.{d.entry_type} does not allow a spoken confirmation to sign"
            )
        if pres.expires_at is not None and _now() > pres.expires_at:
            raise AttestRefused("the presentation expired; present it again")
        if digest(_statement(d, version, s)) != pres.digest:
            raise AttestRefused("the draft changed since it was presented; present it again")
        response_id = str(uuid.uuid4())
        s.add(
            AttestResponse(
                response_id=response_id,
                presentation_id=presentation_id,
                principal=handle,
                answer=answer,
                via=via,
                transcript=transcript,
                audio_sha256=audio_sha256,
                latency_ms=latency_ms,
                console_id=console_id,
                correction=_normalised(correction) if correction else None,
                at=_now(),
            )
        )
        modality = pres.modality
        draft_id = d.draft_id
        s.commit()
    nxt = None
    if answer == "ask" and correction:
        fill(draft_id, correction, by=Signatory(principal=principal, roles=()))
        nxt = present(draft_id, modality=modality)
    return Response(
        response_id=response_id,
        presentation_id=presentation_id,
        answer=answer,
        via=via,
        next_presentation=nxt,
    )


def presentation_context(presentation_id: str) -> dict[str, Any]:
    """What a grant is minted against: the presentation's digest, the draft's logbook, type,
    meaning, person and status, and ``assurance``, the :class:`SigningRules` for its kind."""
    with store.session_scope() as s:
        pres = s.get(AttestPresentation, presentation_id)
        if pres is None:
            raise AttestRefused(f"no presentation {presentation_id}")
        d = _load_draft(s, pres.draft_id)
        version, et = _entry_type(d.logbook, d.entry_type)
        return {
            "presentation_id": presentation_id,
            "digest": pres.digest,
            "site_id": d.site_id,
            "draft_id": d.draft_id,
            "logbook": d.logbook,
            "logbook_version": version,
            "entry_type": et,
            "kind": _kind(d),
            "target": d.target,
            "meaning": d.meaning,
            "for_principal": d.for_principal,
            "status": d.status,
            "assurance": _rules(d, registry.get(d.logbook), et),
        }


# -- signing -------------------------------------------------------------------


def _lock_head(s: Any, site_id: str, logbook: str) -> tuple[int, str]:
    s.execute(
        text(
            "INSERT INTO attest_chain_heads (site_id, logbook, seq, digest) "
            "VALUES (:site, :logbook, 0, :genesis) ON CONFLICT DO NOTHING"
        ),
        {"site": site_id, "logbook": logbook, "genesis": GENESIS},
    )
    row = s.execute(
        text(
            "SELECT seq, digest FROM attest_chain_heads "
            "WHERE site_id = :site AND logbook = :logbook FOR UPDATE"
        ),
        {"site": site_id, "logbook": logbook},
    ).one()
    return int(row.seq), row.digest


def _presented(s: Any, draft_id: str, statement_digest: str) -> list[AttestPresentation]:
    return list(
        s.execute(
            select(AttestPresentation)
            .where(
                AttestPresentation.draft_id == draft_id,
                AttestPresentation.digest == statement_digest,
            )
            .order_by(AttestPresentation.created_at)
        ).scalars()
    )


def _assurance(p: PrincipalContext, claims: dict[str, Any] | None) -> dict[str, Any]:
    if claims is None:
        return {"posture": p.posture, "idp": p.idp}
    return {
        "posture": claims["posture"],
        "idp": claims.get("idp"),
        "auth_time": claims.get("auth_time"),
        "amr": claims.get("amr", []),
        "fresh_within_met": claims.get("fresh_within_met"),
        "grant_id": claims["grant_id"],
        "device_id": claims["device_id"],
        "device_class": claims["device_class"],
        "console_id": claims.get("console_id"),
        "location": claims.get("location"),
        "presence": claims.get("presence"),
    }


def sign(
    presentation_id: str,
    *,
    signatory: Signatory,
    signer: Signer,
    source: str = "cli",
    occurred_at: datetime | None = None,
    grant: str | None = None,
    grant_keys: dict[str, bytes] | None = None,
) -> SignResult:
    """Chain a record for a presentation the person answered ``sign``.

    With ``grant`` (the browser path, ADR-146) the token is verified against
    ``grant_keys``, must be for this presentation, digest and person, and is
    used up. Without one, only a type that needs no fresh sign-in, no presence
    and accepts a personal device can be signed (the CLI path)."""
    handle = signatory.principal.handle
    claims: dict[str, Any] | None = None
    if grant is not None:
        from .grants import read as read_grant

        claims = read_grant(grant, grant_keys or {})
    with store.session_scope() as s:
        pres = s.get(AttestPresentation, presentation_id)
        if pres is None:
            raise AttestRefused(f"no presentation {presentation_id}")
        d = _load_draft(s, pres.draft_id, lock=True)
        if d.status == "signed":
            raise AttestRefused(f"draft {d.draft_id} is already signed")
        if d.status != "open":
            raise AttestRefused(f"draft {d.draft_id} is {d.status}")
        if handle != d.for_principal:
            raise AttestRefused(f"draft {d.draft_id} is for {d.for_principal}, not {handle}")
        version, et = _entry_type(d.logbook, d.entry_type)
        if digest(_statement(d, version, s)) != pres.digest:
            raise AttestRefused("the draft changed since it was presented; present it again")
        rules = _rules(d, registry.get(d.logbook), et)
        if claims is None and (
            rules.fresh_within_seconds is not None
            or rules.presence is not None
            or "personal" not in rules.sign_devices
        ):
            raise AttestRefused(f"{rules.what} is signed only with a signing grant (ADR-146)")
        if claims is not None:
            if claims["presentation_id"] != presentation_id or claims["digest"] != pres.digest:
                raise AttestRefused("the grant is not for this presentation")
            if claims["principal"] != handle:
                raise AttestRefused(f"the grant is for {claims['principal']}, not {handle}")
            if (claims["logbook"], claims["meaning"]) != (d.logbook, d.meaning):
                raise AttestRefused("the grant is not for this logbook and meaning")
        answer = s.execute(
            select(AttestResponse)
            .where(
                AttestResponse.presentation_id == presentation_id,
                AttestResponse.principal == handle,
            )
            .order_by(AttestResponse.at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if answer is None or answer.answer != "sign":
            said = "no answer" if answer is None else f"answered {answer.answer!r}"
            raise AttestRefused(f"{handle} has {said} to this presentation; only 'sign' signs")
        presented = _presented(s, d.draft_id, pres.digest)
        missing = sorted(set(et.confirm.modalities_all) - {p.modality for p in presented})
        if missing:
            raise AttestRefused(
                f"{d.logbook}.{d.entry_type} must also be presented by {missing} before it is signed"
            )

        posture = claims["posture"] if claims is not None else signatory.principal.posture
        principal_ctx = PrincipalContext(
            handle=handle,
            posture=posture,
            assured=True,
            idp=claims.get("idp") if claims is not None else signatory.principal.idp,
        )
        if posture not in SIGNING_POSTURES or not principal_ctx.meets(rules.posture):
            raise AttestRefused(
                f"posture {posture!r} cannot sign {rules.what}; it needs {rules.posture!r} or higher"
            )
        authority = [
            r for r in rules.roles if r in signatory.roles and r not in PLATFORM_ADMIN_ROLES
        ]
        if not authority:
            raise AttestRefused(
                f"{handle} holds no role that may sign {rules.what} (needs one of {list(rules.roles)})"
            )

        now = _now()
        seq, prev = _lock_head(s, d.site_id, d.logbook)
        if _kind(d) == "ack":
            # Under the chain lock, so two presentations of one person's acknowledgement cannot
            # both sign (unit L3).
            target_row = _target_row(s, d)
            if target_row.signer == handle:
                raise AttestRefused(
                    f"{handle} signed record {d.target}; it is not acknowledged by its signer"
                )
            earlier = _acknowledged_by(s, d.target, handle)
            if earlier is not None:
                raise AttestRefused(
                    f"record {d.target} is already acknowledged by you at #{earlier.seq}"
                )
        if _kind(d) == "cosign":
            # Separation of duties and one co-signature per entry, under the chain lock (unit L4).
            target_row = _target_row(s, d)
            if target_row.signer == handle:
                raise AttestRefused(
                    f"{handle} signed record {d.target}; it is not co-signed by its signer"
                )
            earlier = _cosigned(s, d.target)
            if earlier is not None:
                raise AttestRefused(
                    f"record {d.target} is already co-signed by {earlier.signer} at #{earlier.seq}"
                )
        attestation_id = str(uuid.uuid4())
        from . import intervals

        kind = _kind(d)
        # An action on an entry never opens, closes or needs an interval: a correction after a
        # run has ended must still be possible (ADR-142 rule 7).
        interval = (
            intervals.enter(s, registry.get(d.logbook), et, d.site_id, attestation_id, now)
            if kind == "statement"
            else None
        )
        action = (
            {}
            if kind == "statement"
            else {
                "target": d.target,
                "target_digest": _target_row(s, d).digest,
                "reason": d.reason,
            }  # fmt: skip
        )
        body = normalise(
            {
                "attestation_id": attestation_id,
                "site_id": d.site_id,
                "logbook": d.logbook,
                "logbook_version": version,
                "kind": kind,
                "meaning": d.meaning,
                "entry_type": d.entry_type if kind == "statement" else None,
                **action,
                "subjects": [],
                "content": d.content,
                "origin": d.origin,
                "source": source,
                "signer": {
                    "principal": handle,
                    "display": signatory.display,
                    "kind": "human",
                    "authority": {"roles": authority},
                },
                "assurance": _assurance(principal_ctx, claims),
                "evidence": {
                    "presentation_digest": pres.digest,
                    "presented": [
                        {
                            "presentation_id": p.presentation_id,
                            "modality": p.modality,
                            "presenter": p.presenter,
                            "text": p.rendered,
                            "at": p.created_at,
                        }
                        for p in presented
                    ],
                    "response": {
                        "response_id": answer.response_id,
                        "presentation_id": presentation_id,
                        "answer": answer.answer,
                        "via": answer.via,
                        "transcript": answer.transcript,
                        "audio_sha256": answer.audio_sha256,
                        "latency_ms": answer.latency_ms,
                        "console_id": answer.console_id,
                        "at": answer.at,
                    },
                    "provenance": d.provenance,
                },
                "occurred_at": occurred_at or now,
                "recorded_at": now,
            }
        )
        if claims is not None:
            s.add(
                AttestGrantUse(
                    grant_id=claims["grant_id"],
                    presentation_id=presentation_id,
                    principal=handle,
                    used_at=now,
                )
            )
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                raise AttestRefused("the grant was already used; request a new one") from None
        if interval is not None:
            body["interval"] = interval
        record = seal(body, seq=seq + 1, prev_digest=prev, signer=signer)
        s.add(
            AttestRecord(
                attestation_id=attestation_id,
                site_id=d.site_id,
                logbook=d.logbook,
                seq=record["seq"],
                digest=record["digest"],
                prev_digest=prev,
                kind=kind,
                meaning=d.meaning,
                entry_type=d.entry_type if kind == "statement" else None,
                target=d.target if kind != "statement" else None,
                signer=handle,
                occurred_at=occurred_at or now,
                recorded_at=now,
                record=record,
                response_id=answer.response_id,
            )
        )
        s.execute(
            text(
                "UPDATE attest_chain_heads SET seq = :seq, digest = :digest "
                "WHERE site_id = :site AND logbook = :logbook"
            ),
            {
                "seq": record["seq"],
                "digest": record["digest"],
                "site": d.site_id,
                "logbook": d.logbook,
            },
        )
        d.status = "signed"
        d.attestation_id = attestation_id
        s.commit()
    _publish_signed(record)
    return SignResult(status="signed", record=record)


def _cosign_decl(rec: dict[str, Any]) -> Any:
    """The ``cosign`` declaration of a statement's type, or None (unknown types included)."""
    if rec.get("kind", "statement") != "statement" or not rec.get("entry_type"):
        return None
    try:
        return registry.get(rec["logbook"]).type(rec["entry_type"]).cosign
    except LogbookError:
        return None


def brief(rec: dict[str, Any], *, cosigned: bool | None = None) -> dict[str, Any]:
    """The summary of a record that lists and the live stream carry.

    ``cosign`` is ``"pending"`` or ``"done"`` for a statement whose type declares a co-signature,
    else None (unit L4). Alone, ``brief`` reports ``pending`` for such a statement; a list resolves
    ``done`` in one query through :func:`briefs`, and :func:`thread` from the thread itself."""
    decl = _cosign_decl(rec)
    return {
        "attestation_id": rec["attestation_id"],
        "seq": rec["seq"],
        "logbook": rec["logbook"],
        "site_id": rec["site_id"],
        "entry_type": rec.get("entry_type"),
        "meaning": rec["meaning"],
        "title": (rec.get("content") or {}).get("title"),
        "signer": rec["signer"]["principal"],
        "occurred_at": rec["occurred_at"],
        "digest": rec["digest"],
        "kind": rec.get("kind", "statement"),
        "target": rec.get("target"),
        "cosign": None if decl is None else ("done" if cosigned else "pending"),
        # {kind, number[, event]} for a record signed inside a declared interval; null outside any.
        "interval": rec.get("interval"),
    }


def briefs(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """:func:`brief` of each record, with ``cosign`` resolved against the store in one query."""
    wanted = [r["attestation_id"] for r in recs if _cosign_decl(r) is not None]
    done: set[str] = set()
    if wanted:
        with store.session_scope() as s:
            done = set(
                s.execute(
                    select(AttestRecord.target).where(
                        AttestRecord.kind == "cosign", AttestRecord.target.in_(wanted)
                    )
                ).scalars()
            )
    return [brief(r, cosigned=r["attestation_id"] in done) for r in recs]


def _publish_signed(rec: dict[str, Any]) -> None:
    """``attest.<logbook>.signed`` on the process bus, after the commit, from
    every signing path. Best effort: the record is the truth and the stream
    replays from the store on reconnect, so a lost event costs latency and
    never a record. A transactional outbox replaces this when the bus has one."""
    try:
        from axiom.infra.bus import get_default_eventbus

        bus = get_default_eventbus()
        bus.publish(
            f"attest.{rec['logbook']}.signed", {"schema_version": 1, **brief(rec)}, source="attest"
        )
        if rec.get("kind") == "supplement":
            bus.publish(
                f"attest.{rec['logbook']}.supplemented",
                {"schema_version": 1, **brief(rec), "reason": rec["reason"],
                 "target_digest": rec["target_digest"]},
                source="attest",
            )  # fmt: skip
        elif rec.get("kind") == "cosign":
            bus.publish(
                f"attest.{rec['logbook']}.cosigned",
                {"schema_version": 1, **brief(rec), "target_digest": rec["target_digest"]},
                source="attest",
            )
        decl = _cosign_decl(rec)
        if decl is not None:
            from . import cosign as cosign_mod

            b = brief(rec)
            bus.publish(
                f"attest.{rec['logbook']}.cosign_requested",
                {"schema_version": 1, **b, "cosign_roles": list(decl.roles)},
                source="attest",
            )
            cosign_mod.request(b, decl.roles)
    except Exception:  # noqa: BLE001 - the signature is committed; the stream replays
        pass


# -- reading and verifying ---------------------------------------------------------


def records(site_id: str, logbook: str, *, from_seq: int = 1) -> list[dict[str, Any]]:
    """Signed records of one chain, in order."""
    with store.session_scope() as s:
        rows = s.execute(
            select(AttestRecord.record)
            .where(
                AttestRecord.site_id == site_id,
                AttestRecord.logbook == logbook,
                AttestRecord.seq >= from_seq,
            )
            .order_by(AttestRecord.seq)
        ).scalars()
        return list(rows)


#: The most records one page may hold (unit W3): a surface reads the newest 200 and pages by
#: sequence from there, never the whole chain.
PAGE_LIMIT = 200


def text_query(text_: str) -> str | None:
    """The ``to_tsquery('simple', ...)`` pattern for a person's words: each word a prefix, all
    required, so "pump noted" finds "pumps were noted". None when there is no word in it."""
    words = re.findall(r"\w+", text_ or "", flags=re.UNICODE)
    return " & ".join(f"{w.lower()}:*" for w in words) or None


def records_where(
    site_id: str,
    logbook: str,
    *,
    from_seq: int = 1,
    before_seq: int | None = None,
    after_seq: int | None = None,
    limit: int | None = None,
    entry_types: Iterable[str] | None = None,
    meanings: Iterable[str] | None = None,
    signer: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    interval: tuple[str, Any] | None = None,
    text_: str | None = None,
) -> list[dict[str, Any]]:
    """Signed records of one chain that match the filters, in ``seq`` order, read with one
    statement that the database answers from its indexes (unit W3): every filter is a WHERE
    clause, the page is a keyset on ``seq`` (``before_seq`` for older rows, ``after_seq`` for
    newer, the direction a live tail reads) and ``limit`` is applied by the database. Nothing is
    read and then filtered or sliced here. Without ``limit`` it is the whole matching chain, as
    the verifier and the exports need it."""
    from sqlalchemy import func, literal_column

    stmt = select(AttestRecord.record).where(
        AttestRecord.site_id == site_id,
        AttestRecord.logbook == logbook,
        AttestRecord.seq >= from_seq,
    )
    if before_seq is not None:
        stmt = stmt.where(AttestRecord.seq < before_seq)
    if after_seq is not None:
        stmt = stmt.where(AttestRecord.seq > after_seq)
    if entry_types is not None:
        stmt = stmt.where(AttestRecord.entry_type.in_(list(entry_types)))
    if meanings is not None:
        stmt = stmt.where(AttestRecord.meaning.in_(list(meanings)))
    if signer:
        stmt = stmt.where(AttestRecord.signer == signer)
    if since is not None:
        stmt = stmt.where(AttestRecord.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(AttestRecord.occurred_at <= until)
    if interval is not None:
        kind, number = interval
        stmt = stmt.where(
            AttestRecord.record["interval"]["kind"].astext == str(kind),
            AttestRecord.record["interval"]["number"].astext == str(number),
        )
    if text_:
        pattern = text_query(text_)
        if pattern is None:
            return []
        stmt = stmt.where(
            literal_column("search_vector").op("@@")(func.to_tsquery("simple", pattern))
        )
    if limit is None:
        stmt = stmt.order_by(AttestRecord.seq)
    elif after_seq is not None:
        stmt = stmt.order_by(AttestRecord.seq).limit(int(limit))
    else:
        # The newest page: read newest first from the index, hand it back in chain order.
        stmt = stmt.order_by(AttestRecord.seq.desc()).limit(int(limit))
    with store.session_scope() as s:
        rows = list(s.execute(stmt).scalars())
    rows.sort(key=lambda r: int(r["seq"]))
    return rows


def record(attestation_id: str) -> dict[str, Any] | None:
    """One signed record by its public id."""
    with store.session_scope() as s:
        row = s.get(AttestRecord, attestation_id)
        return None if row is None else row.record


def thread(attestation_id: str) -> list[dict[str, Any]]:
    """The record and every later record in its chain that acts on it, directly or through
    another one (a supplement of a supplement), in chain order, as :func:`brief` dicts with
    ``reason`` and ``content`` added. Empty
    when there is no such record. A reader takes a field's effective value from the last record
    in the thread that carries it (design note L2, section 6)."""
    with store.session_scope() as s:
        root = s.get(AttestRecord, attestation_id)
        if root is None:
            return []
        later = s.execute(
            select(AttestRecord.attestation_id, AttestRecord.target, AttestRecord.record)
            .where(
                AttestRecord.site_id == root.site_id,
                AttestRecord.logbook == root.logbook,
                AttestRecord.seq > root.seq,
                AttestRecord.target.is_not(None),
            )
            .order_by(AttestRecord.seq)
        ).all()
        ids = {root.attestation_id}
        items = [root.record]
        for row in later:
            if row.target in ids:
                ids.add(row.attestation_id)
                items.append(row.record)
        cosigned = {r["target"] for r in items if r.get("kind") == "cosign"}
        return [_thread_item(r, cosigned=r["attestation_id"] in cosigned) for r in items]


def pending_cosigns(
    site_id: str,
    logbook: str,
    *,
    for_roles: tuple[str, ...] | list[str] | None = None,
    older_than: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The statements of one chain whose type declares a co-signature and that no ``cosign``
    record points at, in chain order as :func:`brief` dicts with ``age_seconds`` (unit L4).
    ``for_roles`` keeps those a holder of one of the given roles may co-sign; ``older_than``
    (seconds) those at least that old. A query over the records and the registry."""
    cosigns = select(AttestRecord.target).where(
        AttestRecord.site_id == site_id,
        AttestRecord.logbook == logbook,
        AttestRecord.kind == "cosign",
    )
    q = (
        select(AttestRecord.record)
        .where(
            AttestRecord.site_id == site_id,
            AttestRecord.logbook == logbook,
            AttestRecord.kind == "statement",
            AttestRecord.attestation_id.not_in(cosigns),
        )
        .order_by(AttestRecord.seq)
    )
    moment = now or _now()
    out: list[dict[str, Any]] = []
    with store.session_scope() as s:
        for rec in s.execute(q).scalars():
            decl = _cosign_decl(rec)
            if decl is None:
                continue
            if for_roles is not None and not set(for_roles) & set(decl.roles):
                continue
            recorded = rec.get("recorded_at")
            at = (
                datetime.fromisoformat(str(recorded).replace("Z", "+00:00")) if recorded else moment
            )
            age = max(0, int((moment - at).total_seconds()))
            if older_than is not None and age < older_than:
                continue
            out.append(
                {**brief(rec, cosigned=False), "age_seconds": age, "cosign_roles": list(decl.roles)}
            )
    return out


def unacknowledged(
    site_id: str,
    logbook: str,
    *,
    by: str | None = None,
    since_seq: int | None = None,
) -> list[dict[str, Any]]:
    """The entries (statements and supplements) of one chain that nobody has acknowledged, or
    with ``by``, that ``by`` has not acknowledged, in chain order as :func:`brief` dicts (unit
    L3). A query over the records: there is no second table."""
    acks = select(AttestRecord.target).where(
        AttestRecord.site_id == site_id,
        AttestRecord.logbook == logbook,
        AttestRecord.kind == "ack",
    )
    if by:
        acks = acks.where(AttestRecord.signer == by)
    q = (
        select(AttestRecord.record)
        .where(
            AttestRecord.site_id == site_id,
            AttestRecord.logbook == logbook,
            AttestRecord.kind.in_(ACKNOWLEDGEABLE_KINDS),
            AttestRecord.attestation_id.not_in(acks),
        )
        .order_by(AttestRecord.seq)
    )
    if since_seq is not None:
        q = q.where(AttestRecord.seq >= since_seq)
    with store.session_scope() as s:
        return [brief(r) for r in s.execute(q).scalars()]


def _thread_item(rec: dict[str, Any], *, cosigned: bool = False) -> dict[str, Any]:
    """A thread row: the brief, plus what a reader needs to show the entry without fetching
    each record (its ``content``, and an action's ``reason``)."""
    return {
        **brief(rec, cosigned=cosigned),
        "reason": rec.get("reason"),
        "content": rec.get("content"),
    }


def verify_logbook(site_id: str, logbook: str, public_keys: dict[str, bytes]) -> VerifyReport:
    """Walk one chain from genesis and name the first broken record."""
    return verify_chain(records(site_id, logbook), public_keys)


# -- anchors ---------------------------------------------------------------------


def anchor_site(site_id: str, signer: Signer) -> dict[str, Any]:
    """Sign a Merkle root over every logbook head at ``site_id`` and store it."""
    with store.session_scope() as s:
        rows = s.execute(
            text(
                "SELECT logbook, seq, digest FROM attest_chain_heads "
                "WHERE site_id = :site AND seq > 0 ORDER BY logbook"
            ),
            {"site": site_id},
        ).all()
        if not rows:
            raise AttestRefused(f"no chains at {site_id} to anchor")
        heads = [{"logbook": r.logbook, "seq": int(r.seq), "digest": r.digest} for r in rows]
        root = merkle_root(heads)
        anchor = {
            "anchor_id": str(uuid.uuid4()),
            "site_id": site_id,
            "created_at": _now(),
            "root": root,
            "heads": heads,
            "node_sig": sign_anchor(root, signer),
        }
        s.add(AttestAnchor(**anchor))
        s.commit()
        return anchor


def anchors(site_id: str) -> list[dict[str, Any]]:
    with store.session_scope() as s:
        rows = s.execute(
            select(AttestAnchor)
            .where(AttestAnchor.site_id == site_id)
            .order_by(AttestAnchor.created_at)
        ).scalars()
        return [
            {
                "anchor_id": a.anchor_id,
                "site_id": a.site_id,
                "created_at": a.created_at,
                "root": a.root,
                "heads": a.heads,
                "node_sig": a.node_sig,
            }
            for a in rows
        ]


def verify_site_anchor(anchor_id: str, public_keys: dict[str, bytes]) -> tuple[bool, str | None]:
    """Check an anchor's signature and root, then that every head it fixed is
    still the record at that position. ``head_rewritten:<logbook>`` means the
    chain was rewritten after the anchor, even if it verifies on its own."""
    with store.session_scope() as s:
        a = s.get(AttestAnchor, anchor_id)
        if a is None:
            raise AttestRefused(f"no anchor {anchor_id}")
        ok, reason = verify_anchor(a.root, a.heads, a.node_sig, public_keys)
        if not ok:
            return ok, reason
        for head in a.heads:
            current = s.execute(
                select(AttestRecord.record).where(
                    AttestRecord.site_id == a.site_id,
                    AttestRecord.logbook == head["logbook"],
                    AttestRecord.seq == head["seq"],
                )
            ).scalar_one_or_none()
            if current is None or current.get("digest") != head["digest"]:
                return False, f"head_rewritten:{head['logbook']}"
    return True, None


__all__ = [
    "ACKNOWLEDGEABLE_KINDS",
    "COSIGN_MEANING",
    "AttestRefused",
    "SigningRules",
    "anchor_site",
    "anchors",
    "brief",
    "briefs",
    "verify_site_anchor",
    "Presentation",
    "RESPONSES",
    "Response",
    "respond",
    "SignResult",
    "Signatory",
    "SUPPLEMENT_MEANINGS",
    "SUPPLEMENT_REASONS",
    "create_ack_draft",
    "create_cosign_draft",
    "create_draft",
    "create_supplement_draft",
    "draft",
    "normalised_content",
    "fill",
    "present",
    "presentation_context",
    "pending_cosigns",
    "record",
    "records",
    "root_entry_type",
    "rules_for",
    "sign",
    "thread",
    "unacknowledged",
    "verify_logbook",
]
