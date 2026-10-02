# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/attest``: logbooks, drafts, confirmation, signing and the live stream.

Every route needs a gate session; the person is named the way receipts and
the gate name them (``principal_from_idp_subject(sub, site)``), so a draft,
a grant and a signature all refer to one handle. A person reads and writes
only their own drafts. Signing takes a grant from ``POST /gate/grants``
(ADR-146); there is no other way to sign over HTTP.

Reads of records are scoped to the sites this node serves and the session's
site claim, through :mod:`axiom.infra.site_scope`.

The stream (``GET /{logbook}/stream``, ADR-147) sends one event per signed record,
with ``id`` = the record's sequence number. A client that reconnects with
``Last-Event-ID`` is first sent every record after that number from the
store, so nothing signed while it was away is lost.

No ``from __future__ import annotations`` here: FastAPI must resolve the
locally imported ``Request`` annotation at definition time.
"""

import asyncio
import json
import time
from typing import Any

from axiom.infra.site_scope import deployment_sites

from . import registry, service, signing
from .logbooks import LogbookError
from .roles import roles_for
from .service import AttestRefused, Signatory

HEARTBEAT_SECONDS = 10.0


def _person(request) -> dict[str, Any] | None:
    """``{handle, site, idp, posture, display}`` for the gate session, or None."""
    from axiom.extensions.builtins.fleet.api import _session_resolver_factory
    from axiom.infra.principal import principal_from_idp_subject

    resolver = _session_resolver_factory()
    credential = resolver(request) if resolver is not None else None
    claims = getattr(credential, "claims", None) or {}
    sub = claims.get("sub")
    if not sub:
        return None
    site = str(claims.get("site") or "")
    try:
        handle = principal_from_idp_subject(str(sub), site)
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


def register_routes(router: Any, *, subpath: str = "/attest") -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse

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
        return person

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
        request: Request, logbook: str, site: str | None = None, limit: int = 50
    ):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        recs = service.records(the_site, logbook)
        return {"site_id": the_site, "logbook": logbook,
                "records": [_brief(r) for r in recs[-max(1, min(limit, 500)):]]}  # fmt: skip

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
        return {"record": rec, "uri": f"axiom://attest/sha256:{rec['digest']}"}

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
    async def verify(request: Request, logbook: str, site: str | None = None):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        report = service.verify_logbook(the_site, logbook, signing.public_keys())
        return {
            "site_id": the_site,
            "logbook": logbook,
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
                        kind, payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except TimeoutError:
                        if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                            last_beat = time.monotonic()
                            yield ": heartbeat\n\n"
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


__all__ = ["HEARTBEAT_SECONDS", "register_routes"]
