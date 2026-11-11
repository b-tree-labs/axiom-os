# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/attest``: logbooks, drafts, confirmation, signing and the live stream.

Every route but ``POST /proposals`` needs a gate session; the person is named the way receipts and
the gate name them (``principal_from_idp_subject(sub, site)``), so a draft,
a grant and a signature all refer to one handle. A person reads and writes
only their own drafts. Signing takes a grant from ``POST /gate/grants``
(ADR-146); there is no other way to sign over HTTP. ``POST /proposals`` is the
reverse: software only, by Bearer API key, and it only drafts (unit L1).

``POST /{logbook}/records/{id}/supplement`` drafts a supplement to a signed record for the
session's person; it is signed through the same present, respond and sign routes (unit L2).
``POST .../records/{id}/ack`` drafts an acknowledgement the same way, and
``GET /{logbook}/records?unacknowledged=1`` lists the entries nobody has acknowledged (L3).
``POST .../records/{id}/cosign`` drafts a co-signature and ``?cosign=pending`` lists what waits
for one (L4).

Reads of records are scoped to the sites this node serves and the session's
site claim, through :mod:`axiom.infra.site_scope`. ``GET /{logbook}/records``
takes the filters of :mod:`.filters` (``entry_type``, ``meaning``, ``signer``,
``from``, ``to``, ``interval=kind:number``, ``q``), which narrow the site's own
records and never widen the site; ``GET /{logbook}/intervals`` lists a
logbook's intervals with their counts and the gaps between them (unit W3).

The stream (``GET /{logbook}/stream``, ADR-147) sends one event per signed record,
with ``id`` = the record's sequence number. A client that reconnects with
``Last-Event-ID`` is first sent every record after that number from the
store, so nothing signed while it was away is lost. While the chain is quiet
the stream beats every ``HEARTBEAT_SECONDS``: a comment frame (``: heartbeat``,
which a proxy keeps an idle connection open on) and a named ``heartbeat`` event
carrying the server time, which is the one a browser can see (``EventSource``
never surfaces a comment), so a page can tell a silent link from a quiet chain
(unit W4). A heartbeat has no ``id`` and never moves the resume point.

No ``from __future__ import annotations`` here: FastAPI must resolve the
locally imported ``Request`` annotation at definition time.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

from axiom.infra.site_scope import deployment_sites

from . import filters, intervals, reads, registry, service, signing
from .logbooks import LogbookError
from .roles import roles_for
from .service import AttestRefused, Signatory

HEARTBEAT_SECONDS = 10.0

#: The scope a Bearer key needs to propose. No scope-naming convention exists yet, so it is the
#: skill's name (design note L1, section 2).
PROPOSE_SCOPE = "attest.propose"


def _person(request) -> dict[str, Any] | None:
    """``{handle, site, idp, posture, display}`` for the gate session, or None.

    A session whose sign-in cannot be made a handle is still a session: it
    answers ``{"unnamed": <why>}``, so the door can say what is wrong with the
    email instead of asking a signed-in person to sign in.
    """
    from axiom.extensions.builtins.fleet.api import _session_resolver_factory

    from .naming import SignerNameError, signer_handle, signer_site

    resolver = _session_resolver_factory()
    credential = resolver(request) if resolver is not None else None
    claims = getattr(credential, "claims", None) or {}
    if not claims.get("sub"):
        return None
    # Named by the one rule the grant endpoint also uses (naming.py), so a
    # grant is always for the person a draft is for.
    site = signer_site(claims) or ""
    try:
        handle = signer_handle(claims)
    except SignerNameError as exc:
        return {"unnamed": str(exc)}
    except ValueError:
        return None
    idp = claims.get("idp")
    return {
        "handle": handle,
        "site": site or None,
        "idp": idp,
        "posture": "sso" if idp else "attested",
        "display": str(claims.get("name") or ""),
    }


def _site_for(person: dict[str, Any], requested: str | None) -> str:
    """The one site a request acts on: the session's site, narrowed by what
    this node serves. A request for another site is refused, never widened."""
    served = deployment_sites()
    site = requested or person["site"]
    if not site and served is not None and len(served) == 1:
        site = next(iter(served))
    if not site:
        raise PermissionError("name the site")
    if person["site"] and site != person["site"]:
        raise PermissionError(f"this session is for {person['site']}, not {site}")
    if served is not None and site not in served:
        raise PermissionError(f"this node does not serve {site}")
    return site


def _presentation_json(p: service.Presentation) -> dict[str, Any]:
    return {
        "presentation_id": p.presentation_id,
        "digest": p.digest,
        "modality": p.modality,
        "presenter": p.presenter,
        "forms": p.forms,
        "speakable": p.speakable,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
    }


def _instruments(f, site: str | None) -> list[dict[str, Any]] | None:
    """A readings field's instruments at ``site``, for the form to render one
    input per instrument. None when the field has no source or no site."""
    if getattr(f, "type", "") != "readings" or not getattr(f, "from_site", "") or not site:
        return None
    from . import field_sources

    try:
        return [
            {"id": i.id, "label": i.label, "unit": i.unit}
            for i in field_sources.resolve(f.from_site, site)
        ]
    except LookupError:
        return None


def _logbook_json(logbook, site: str | None = None) -> dict[str, Any]:
    return {
        "id": logbook.id,
        "version": logbook.version,
        "display": logbook.display,
        "not_yet_enforced": list(logbook.not_yet_enforced),
        # As declared, or None: a surface says "not declared", never a default.
        "retention": logbook.retention,
        # The intervals the log declares, so a surface can offer the Intervals view (unit W3).
        "intervals": [
            {"kind": k, "opens": list(d.opens), "closes": list(d.closes)}
            for k, d in logbook.intervals.items()
        ],
        "types": [
            {
                "id": t.id,
                "meanings": list(t.meanings),
                "roles": list(t.roles),
                "posture": t.posture,
                "fresh_within_seconds": t.fresh_within_seconds,
                "sign_devices": list(t.sign_devices),
                "presence": t.presence,
                "confirm": {
                    "modalities_any": list(t.confirm.modalities_any),
                    "modalities_all": list(t.confirm.modalities_all),
                    "voice_confirm_allowed": t.confirm.voice_confirm_allowed,
                    "timeout_seconds": t.confirm.timeout_seconds,
                    "read_back": t.confirm.read_back,
                },
                "fields": [
                    {
                        "id": f.id,
                        "type": f.type,
                        "label": f.label,
                        "required": f.required,
                        "observe": f.observe,
                        "unit": f.unit,
                        "choices": list(f.choices),
                        "from_site": f.from_site or None,
                        "instruments": _instruments(f, site),
                    }
                    for f in t.fields
                ],
            }
            for t in logbook.types
        ],
    }


def _sse(event_id: int | None, event: str, data: dict[str, Any]) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


_brief = service.brief


def _page_etag(rows: list[dict[str, Any]]) -> str:
    """A page's identity: the sequences and digests it holds. The same page answers 304."""
    import hashlib

    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r.get('seq')}:{r.get('digest')}\n".encode())
    return f'W/"{h.hexdigest()[:32]}"'


def register_routes(router: Any, *, subpath: str = "/attest") -> None:
    from fastapi import Query, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    def refuse(status: int, detail: str, code: str = "refused") -> JSONResponse:
        return JSONResponse({"detail": detail, "code": code}, status_code=status)

    async def body_of(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def who(request: Request) -> dict[str, Any] | JSONResponse:
        person = _person(request)
        if person is None:
            return refuse(401, "sign in first", "no_session")
        if "unnamed" in person:
            return refuse(403, person["unnamed"], "signer_name")
        return person

    def _read(person: dict[str, Any], what: str, site: str | None, logbook: str, *, ids=(),
              count=None, params=None) -> None:  # fmt: skip
        """A read of signed records, recorded at the door (reads.py, unit X2). Never raises."""
        reads.record_access(
            state_dir=_state_dir(), principal=person["handle"], what=what, site=site,
            logbook=logbook, ids=ids, count=count, surface="web", params=params,
        )  # fmt: skip

    def own_draft(person: dict[str, Any], draft_id: str) -> dict[str, Any] | JSONResponse:
        try:
            d = service.draft(draft_id)
        except AttestRefused:
            return refuse(404, f"no draft {draft_id}", "not_found")
        if d["for_principal"] != person["handle"]:
            # Someone else's draft does not exist, as far as this person can tell.
            return refuse(404, f"no draft {draft_id}", "not_found")
        return d

    # -- logbooks --------------------------------------------------------------------

    @router.get(subpath + "/logbooks", tags=["attest"])
    async def list_logbooks(request: Request):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        return {"logbooks": [_logbook_json(b) for b in registry.all_logbooks()]}

    @router.get(subpath + "/logbooks/{logbook}", tags=["attest"])
    async def get_logbook(request: Request, logbook: str, site: str | None = None):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError:
            the_site = None
        try:
            return _logbook_json(registry.get(logbook), the_site)
        except LogbookError as exc:
            return refuse(404, str(exc), "not_found")

    # -- drafts -------------------------------------------------------------------

    @router.post(subpath + "/drafts", tags=["attest"])
    async def create_draft(request: Request):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        body = await body_of(request)
        try:
            site = _site_for(person, body.get("site"))
            content = {"title": body.get("title") or "", "fields": body.get("fields") or {}}
            if body.get("body"):
                content["body"] = body["body"]
            draft_id = service.create_draft(
                site_id=site,
                logbook=str(body.get("logbook") or ""),
                entry_type=str(body.get("entry_type") or ""),
                meaning=str(body.get("meaning") or ""),
                content=content,
                origin="human",
                for_principal=person["handle"],
            )
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return JSONResponse(service.draft(draft_id), status_code=201)

    @router.get(subpath + "/drafts", tags=["attest"])
    async def list_drafts(
        request: Request,
        origin: str | None = None,
        status: str = "open",
        logbook: str | None = None,
        site: str | None = None,
        limit: int = 50,
    ):
        """The session person's own tray. ``origin`` is a prefix (``agent``, ``agent:x``)."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        rows = service.list_drafts(
            for_principal=person["handle"], site_id=site, logbook=logbook,
            origin_prefix=origin, status=status or None, limit=limit,
        )  # fmt: skip
        return {"for_principal": person["handle"], "drafts": rows}

    @router.delete(subpath + "/drafts/{draft_id}", tags=["attest"])
    async def discard_draft(request: Request, draft_id: str):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        d = own_draft(person, draft_id)
        if isinstance(d, JSONResponse):
            return d
        try:
            service.discard(draft_id, by=person["handle"])
        except AttestRefused as exc:
            return refuse(409, str(exc), "conflict")
        return service.draft(draft_id)

    # -- proposals: software puts a draft in a person's tray (unit L1) ---------------

    @router.post(subpath + "/proposals", tags=["attest"])
    async def propose(request: Request):
        """A Bearer API key only, never a person's session. The key's principal gives the origin
        (an installed extension or a roster agent), its bound site the site, and it must carry
        the scope ``attest.propose``."""
        from axiom.extensions.builtins.fleet.api import _bearer_identity

        from . import proposals

        if _person(request) is not None:
            return refuse(403, "a person's session cannot propose; write a draft with POST /drafts",
                          "session")  # fmt: skip
        identity = _bearer_identity(request)
        if identity is None:
            return refuse(401, "a proposal needs a Bearer API key", "no_key")
        if PROPOSE_SCOPE not in (identity.scopes or ()):
            return refuse(403, f"the key does not carry the scope {PROPOSE_SCOPE!r}", "scope")
        if not identity.site:
            return refuse(403, "the key is not bound to a site", "site")
        served = deployment_sites()
        if served is not None and identity.site not in served:
            return refuse(403, f"this node does not serve {identity.site}", "site")
        body = await body_of(request)
        if "origin" in body:
            return refuse(400, "origin is not accepted: it comes from the key", "origin")
        try:
            origin = proposals.origin_for_key(identity.principal)
        except AttestRefused as exc:
            return refuse(403, str(exc), "not_software")
        content = {"title": body.get("title") or "", "fields": body.get("fields") or {}}
        if body.get("body"):
            content["body"] = body["body"]
        try:
            d = proposals.propose(
                site_id=identity.site,
                logbook=str(body.get("logbook") or ""),
                entry_type=str(body.get("entry_type") or ""),
                meaning=str(body.get("meaning") or ""),
                content=content,
                origin=origin,
                for_principal=str(body.get("for_principal") or ""),
                state_dir=_state_dir(),
            )
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return JSONResponse(d, status_code=201)

    @router.get(subpath + "/drafts/{draft_id}", tags=["attest"])
    async def get_draft(request: Request, draft_id: str):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        return own_draft(person, draft_id)

    @router.put(subpath + "/drafts/{draft_id}", tags=["attest"])
    async def fill_draft(request: Request, draft_id: str):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        d = own_draft(person, draft_id)
        if isinstance(d, JSONResponse):
            return d
        body = await body_of(request)
        try:
            service.fill(draft_id, body.get("fields") or {}, by=_signatory(person, ()))
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return service.draft(draft_id)

    @router.post(subpath + "/drafts/{draft_id}/present", tags=["attest"])
    async def present(request: Request, draft_id: str):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        d = own_draft(person, draft_id)
        if isinstance(d, JSONResponse):
            return d
        body = await body_of(request)
        try:
            p = service.present(draft_id, modality=str(body.get("modality") or "screen"))
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return JSONResponse(_presentation_json(p), status_code=201)

    @router.post(subpath + "/presentations/{presentation_id}/respond", tags=["attest"])
    async def respond(request: Request, presentation_id: str):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        body = await body_of(request)
        try:
            r = service.respond(
                presentation_id,
                principal=_signatory(person, ()).principal,
                answer=str(body.get("answer") or ""),
                via=str(body.get("via") or "screen"),
                transcript=body.get("transcript"),
                latency_ms=body.get("latency_ms"),
                console_id=body.get("console_id"),
                correction=body.get("correction"),
            )
        except AttestRefused as exc:
            return refuse(422, str(exc))
        out: dict[str, Any] = {"response_id": r.response_id, "answer": r.answer}
        if r.next_presentation is not None:
            out["next_presentation"] = _presentation_json(r.next_presentation)
        return out

    # -- signing ------------------------------------------------------------------

    @router.post(subpath + "/sign", tags=["attest"])
    async def sign(request: Request):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        body = await body_of(request)
        presentation_id = str(body.get("presentation_id") or "")
        grant = body.get("grant")
        if not grant:
            return refuse(400, "signing over HTTP needs a grant from POST /gate/grants", "no_grant")
        try:
            ctx = service.presentation_context(presentation_id)
            roles = roles_for(person["handle"], ctx["site_id"], state_dir=_state_dir())
            result = service.sign(
                presentation_id,
                signatory=_signatory(person, roles),
                signer=signing.signer(),
                source="screen",
                grant=str(grant),
                grant_keys=signing.public_keys(),
            )
        except AttestRefused as exc:
            code = getattr(exc, "code", "refused")
            return refuse(403, str(exc), code)
        rec = result.record
        return JSONResponse(
            {"record": rec, "uri": f"axiom://attest/sha256:{rec['digest']}"}, status_code=201
        )

    # -- records ------------------------------------------------------------------

    @router.get(subpath + "/{logbook}/records", tags=["attest"])
    async def list_records(
        request: Request,
        logbook: str,
        site: str | None = None,
        limit: int = 50,
        before_seq: int | None = None,
        after_seq: int | None = None,
        unacknowledged: str | None = None,
        cosign: str | None = None,
        entry_type: list[str] | None = Query(None),
        meaning: list[str] | None = Query(None),
        signer: str | None = None,
        from_: str | None = Query(None, alias="from"),
        to: str | None = None,
        interval: str | None = None,
        q: str | None = None,
    ):
        """One page of a logbook's records, newest page by default (unit W3): a keyset on ``seq``
        (``before_seq`` for older rows, ``after_seq`` for newer), ``limit`` capped at
        ``PAGE_LIMIT`` and applied by the database, every filter a WHERE clause, no count. Rows
        are the brief (id, seq, time, type, signer, title, flags); the record is read on open.
        A page answers 304 to ``If-None-Match`` with its own ETag. With ``unacknowledged=1``,
        only the entries nobody has acknowledged (unit L3); with ``cosign=pending``, only those
        waiting for a co-signature (unit L4)."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        try:
            wanted = filters.parse_filters(
                {"entry_type": entry_type, "meaning": meaning, "signer": signer,
                 "from": from_, "to": to, "interval": interval, "q": q}
            )  # fmt: skip
        except ValueError as exc:
            return refuse(400, str(exc), "filter")
        n = max(1, min(int(limit), service.PAGE_LIMIT))
        if unacknowledged in ("1", "true"):
            rows = service.unacknowledged(the_site, logbook)[-n:]
            has_more = False
        elif cosign == "pending":
            rows = service.pending_cosigns(the_site, logbook)[-n:]
            has_more = False
        else:
            # One row past the page says whether there is more, without a count.
            page = wanted.apply(
                logbook, the_site, before_seq=before_seq, after_seq=after_seq, limit=n + 1
            )
            if after_seq is not None:
                has_more, page = len(page) > n, page[:n]
            else:
                has_more, page = len(page) > n, page[-n:]
            rows = service.briefs(page)
        body = {
            "site_id": the_site,
            "logbook": logbook,
            "records": rows,
            "page": {
                "limit": n,
                "before_seq": before_seq,
                "after_seq": after_seq,
                "has_more": has_more,
                "oldest_seq": rows[0]["seq"] if rows else None,
                "newest_seq": rows[-1]["seq"] if rows else None,
            },
        }
        _read(person, "records", the_site, logbook, count=len(rows),
              params={"logbook": logbook, "limit": n, "before_seq": before_seq, "after_seq": after_seq})  # fmt: skip
        etag = _page_etag(rows)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(body, headers={"ETag": etag})

    @router.get(subpath + "/{logbook}/intervals", tags=["attest"])
    async def list_intervals(
        request: Request, logbook: str, site: str | None = None, kind: str | None = None
    ):
        """Every interval the logbook has run at the site, with its counts, and the gaps between
        them, computed here and never from a browser's clock (unit W3)."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        return {
            "site_id": the_site,
            "logbook": logbook,
            **intervals.listing(the_site, logbook, kind=kind),
        }

    @router.get(subpath + "/{logbook}/records/{attestation_id}", tags=["attest"])
    async def get_record(request: Request, logbook: str, attestation_id: str):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        rec = service.record(attestation_id)
        if rec is None or rec["logbook"] != logbook:
            return refuse(404, f"no record {attestation_id} in {logbook}", "not_found")
        try:
            _site_for(person, rec["site_id"])
        except PermissionError:
            return refuse(404, f"no record {attestation_id} in {logbook}", "not_found")
        _read(person, "record", rec["site_id"], logbook, ids=[attestation_id],
              params={"logbook": logbook, "id": attestation_id})  # fmt: skip
        return {
            "record": rec,
            "uri": f"axiom://attest/sha256:{rec['digest']}",
            "thread": service.thread(attestation_id),
        }

    @router.post(subpath + "/{logbook}/records/{attestation_id}/ack", tags=["attest"])
    async def acknowledge(request: Request, logbook: str, attestation_id: str):
        """A draft by which the session's person acknowledges a signed record (unit L3). The
        existing present, respond and sign routes complete it."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        rec = service.record(attestation_id)
        missing = refuse(404, f"no record {attestation_id} in {logbook}", "not_found")
        if rec is None or rec["logbook"] != logbook:
            return missing
        body = await body_of(request)
        try:
            site = _site_for(person, body.get("site") or rec["site_id"])
        except PermissionError:
            return missing
        if site != rec["site_id"]:
            return missing
        try:
            draft_id = service.create_ack_draft(
                site_id=site, target=attestation_id, origin="human",
                for_principal=person["handle"], logbook=logbook,
            )  # fmt: skip
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return JSONResponse(service.draft(draft_id), status_code=201)

    @router.post(subpath + "/{logbook}/records/{attestation_id}/cosign", tags=["attest"])
    async def cosign_record(request: Request, logbook: str, attestation_id: str):
        """A draft by which the session's person co-signs a statement whose type requires it
        (unit L4). The existing present, respond and sign routes complete it; a stale sign-in
        is refused at the gate with ``reauth_required`` and ``max_age``."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        rec = service.record(attestation_id)
        missing = refuse(404, f"no record {attestation_id} in {logbook}", "not_found")
        if rec is None or rec["logbook"] != logbook:
            return missing
        body = await body_of(request)
        try:
            site = _site_for(person, body.get("site") or rec["site_id"])
        except PermissionError:
            return missing
        if site != rec["site_id"]:
            return missing
        try:
            draft_id = service.create_cosign_draft(
                site_id=site, target=attestation_id, origin="human",
                for_principal=person["handle"], logbook=logbook,
            )  # fmt: skip
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return JSONResponse(service.draft(draft_id), status_code=201)

    @router.post(subpath + "/{logbook}/records/{attestation_id}/supplement", tags=["attest"])
    async def supplement(request: Request, logbook: str, attestation_id: str):
        """A draft that corrects, clarifies or adds to a signed record, for the session's person
        (unit L2). The existing present, respond and sign routes complete it."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        rec = service.record(attestation_id)
        missing = refuse(404, f"no record {attestation_id} in {logbook}", "not_found")
        if rec is None or rec["logbook"] != logbook:
            return missing
        body = await body_of(request)
        try:
            site = _site_for(person, body.get("site") or rec["site_id"])
        except PermissionError:
            return missing
        if site != rec["site_id"]:
            return missing
        content: dict[str, Any] = {"fields": body.get("fields") or {}}
        for key in ("title", "body"):
            if body.get(key):
                content[key] = body[key]
        try:
            draft_id = service.create_supplement_draft(
                site_id=site, target=attestation_id, reason=body.get("reason"),
                content=content, origin="human", for_principal=person["handle"], logbook=logbook,
            )  # fmt: skip
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return JSONResponse(service.draft(draft_id), status_code=201)

    @router.get(subpath + "/{logbook}/obligations", tags=["attest"])
    async def obligation_state(request: Request, logbook: str, site: str | None = None):
        """What is due, when, and whether it is ok, due soon or missed."""
        from datetime import UTC, datetime

        from . import obligations

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        try:
            states = obligations.evaluate(the_site, logbook, now=datetime.now(UTC))
        except LogbookError as exc:
            return refuse(404, str(exc), "not_found")
        return {
            "site_id": the_site,
            "logbook": logbook,
            "obligations": [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in st.items()}
                for st in states
            ],
        }

    @router.post(subpath + "/{logbook}/verify", tags=["attest"])
    async def verify(
        request: Request,
        logbook: str,
        site: str | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ):
        """Verify the chain, whole or a slice (``from_seq``, ``to_seq``; unit W3, ADR-241 D7): a
        page verifies the records it holds, starting after the stored digest of ``from_seq - 1``,
        and never walks the whole chain for a surface."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        keys = signing.public_keys()
        if from_seq is None and to_seq is None:
            report = service.verify_logbook(the_site, logbook, keys)
        else:
            from axiom.attest import verify as verify_lib

            try:
                report = verify_lib.chain(the_site, logbook, from_seq, to_seq, public_keys=keys)
            except ValueError as exc:
                return refuse(400, str(exc), "slice")
        _read(person, "verify", the_site, logbook, count=report.checked,
              params={"logbook": logbook, "from_seq": from_seq, "to_seq": to_seq})  # fmt: skip
        return {
            "site_id": the_site,
            "logbook": logbook,
            "from_seq": from_seq,
            "to_seq": to_seq,
            "ok": report.ok,
            "checked": report.checked,
            "head_seq": report.head_seq,
            "head_digest": report.head_digest,
            "first_bad_seq": report.first_bad_seq,
            "reason": report.reason,
        }

    # -- the live stream (ADR-147) ---------------------------------------------------

    @router.get(subpath + "/{logbook}/stream", tags=["attest"])
    async def stream(request: Request, logbook: str, site: str | None = None):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        last = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        try:
            after = int(last) if last else None
        except ValueError:
            after = None
        once = request.query_params.get("once") in ("1", "true")
        # Opening the stream is one read, recorded once; the events it carries are not.
        _read(person, "stream", the_site, logbook, params={"logbook": logbook, "after": after})

        async def events():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            from axiom.infra.bus import get_default_eventbus

            bus = get_default_eventbus()

            def on_event(subject: str, payload: dict[str, Any]) -> None:
                if payload.get("site_id") == the_site:
                    kind = subject.rsplit(".", 1)[-1]
                    loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

            # One token after the logbook: `signed` and `obligation_warn|missed|met`.
            sub = bus.subscribe(f"attest.{logbook}.*", on_event, source="attest.stream")
            try:
                sent = after or 0
                yield _sse(None, "ready", {"logbook": logbook, "site_id": the_site, "after": after})
                if after is not None:
                    for rec in service.records(the_site, logbook, from_seq=after + 1):
                        sent = rec["seq"]
                        yield _sse(rec["seq"], "signed", _brief(rec))
                if once:
                    return
                last_beat = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        kind, payload = await asyncio.wait_for(
                            queue.get(), timeout=min(1.0, HEARTBEAT_SECONDS)
                        )
                    except TimeoutError:
                        if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                            last_beat = time.monotonic()
                            # Two frames, one beat. The comment keeps proxies from closing an
                            # idle connection; the named event is what a browser can see, so
                            # the Log page can tell a silent link from a quiet chain. No id:
                            # a heartbeat never moves the resume point.
                            yield ": heartbeat\n\n"
                            yield _sse(None, "heartbeat", {"at": datetime.now(UTC).isoformat()})
                        continue
                    if kind.startswith("obligation_"):
                        # Not a record: no id, so it never moves the resume point.
                        yield _sse(None, "obligation", payload)
                        continue
                    if kind != "signed" or payload["seq"] <= sent:
                        continue  # already replayed from the store
                    sent = payload["seq"]
                    yield _sse(payload["seq"], "signed", payload)
            finally:
                bus.unsubscribe(sub)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _signatory(person: dict[str, Any], roles) -> Signatory:
    from axiom.infra.principal import PrincipalContext

    return Signatory(
        principal=PrincipalContext(
            handle=person["handle"], posture=person["posture"], assured=True, idp=person["idp"]
        ),
        roles=tuple(roles),
        display=person["display"],
    )


def _state_dir():
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


__all__ = ["HEARTBEAT_SECONDS", "PROPOSE_SCOPE", "register_routes"]
