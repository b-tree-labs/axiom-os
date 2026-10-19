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

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from .logbooks import PLATFORM_ADMIN_ROLES, SIGNING_POSTURES, EntryType, LogbookError

RESPONSES = ("sign", "hold", "ask")


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
) -> str:
    """Record a proposal for ``for_principal`` to sign. Returns the draft id.

    ``origin`` is ``"human"`` when the person typed it, otherwise who proposed
    it (``agent:<id>``, ``integration:<ext>``, ``rule``)."""
    version, et = _entry_type(logbook, entry_type)
    if meaning not in et.meanings:
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
            )
        )
        s.commit()
    return draft_id


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
        d = _load_draft(s, draft_id)
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
        }


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


def _statement(d: AttestDraft, logbook_version: str) -> dict[str, Any]:
    return {
        "site_id": d.site_id,
        "logbook": d.logbook,
        "logbook_version": logbook_version,
        "kind": "statement",
        "entry_type": d.entry_type,
        "meaning": d.meaning,
        "content": d.content,
        "origin": d.origin,
        "for_principal": d.for_principal,
        "provenance": d.provenance,
    }


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
        missing = [f.id for f in et.fields if f.required and f.id not in fields]
        missing += _missing_readings(et, d.site_id, fields)
        if missing:
            raise AttestRefused(f"required fields not entered: {missing}")
        for f in et.fields:
            if f.observe and f.id in fields and not str(d.provenance.get(f.id, "")).startswith("@"):
                raise AttestRefused(f"observe field {f.id!r} was not entered by a person")
        statement = _statement(d, version)
        presenter = presenters.for_type(et, logbook_version=version, site_id=d.site_id)
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
        if digest(_statement(d, version)) != pres.digest:
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
    """What a grant is minted against: the presentation's digest and the
    draft's logbook, type, meaning, person and status."""
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
            "meaning": d.meaning,
            "for_principal": d.for_principal,
            "status": d.status,
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
        if digest(_statement(d, version)) != pres.digest:
            raise AttestRefused("the draft changed since it was presented; present it again")
        if claims is None and (
            et.fresh_within_seconds is not None
            or et.presence is not None
            or "personal" not in et.sign_devices
        ):
            raise AttestRefused(
                f"{d.logbook}.{d.entry_type} is signed only with a signing grant (ADR-146)"
            )
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
        if posture not in SIGNING_POSTURES or not principal_ctx.meets(et.posture):
            raise AttestRefused(
                f"posture {posture!r} cannot sign {d.logbook}.{d.entry_type}; "
                f"it needs {et.posture!r} or higher"
            )
        authority = [r for r in et.roles if r in signatory.roles and r not in PLATFORM_ADMIN_ROLES]
        if not authority:
            raise AttestRefused(
                f"{handle} holds no role that may sign {d.logbook}.{d.entry_type} "
                f"(needs one of {list(et.roles)})"
            )

        now = _now()
        seq, prev = _lock_head(s, d.site_id, d.logbook)
        attestation_id = str(uuid.uuid4())
        from . import intervals

        interval = intervals.enter(s, registry.get(d.logbook), et, d.site_id, attestation_id, now)
        body = normalise(
            {
                "attestation_id": attestation_id,
                "site_id": d.site_id,
                "logbook": d.logbook,
                "logbook_version": version,
                "kind": "statement",
                "meaning": d.meaning,
                "entry_type": d.entry_type,
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
                kind="statement",
                meaning=d.meaning,
                entry_type=d.entry_type,
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


def brief(rec: dict[str, Any]) -> dict[str, Any]:
    """The summary of a record that lists and the live stream carry."""
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
    }


def _publish_signed(rec: dict[str, Any]) -> None:
    """``attest.<logbook>.signed`` on the process bus, after the commit, from
    every signing path. Best effort: the record is the truth and the stream
    replays from the store on reconnect, so a lost event costs latency and
    never a record. A transactional outbox replaces this when the bus has one."""
    try:
        from axiom.infra.bus import get_default_eventbus

        get_default_eventbus().publish(
            f"attest.{rec['logbook']}.signed", {"schema_version": 1, **brief(rec)}, source="attest"
        )
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


def record(attestation_id: str) -> dict[str, Any] | None:
    """One signed record by its public id."""
    with store.session_scope() as s:
        row = s.get(AttestRecord, attestation_id)
        return None if row is None else row.record


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
    "AttestRefused",
    "anchor_site",
    "anchors",
    "brief",
    "verify_site_anchor",
    "Presentation",
    "RESPONSES",
    "Response",
    "respond",
    "SignResult",
    "Signatory",
    "create_draft",
    "draft",
    "fill",
    "present",
    "presentation_context",
    "record",
    "records",
    "sign",
    "verify_logbook",
]
