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
for one (L4). ``GET /{logbook}/seal/preview`` and ``POST /{logbook}/seal`` preview and draft a
close-out of a period; ``/sign`` then also reports the site anchor (L5).

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

from . import access_rules, approvals, changes, filters, intervals, reads, registry, service, signing
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
        # The roles the gate gave this sign-in; they sign only through a site's pair (#1800).
        "gate_roles": tuple(str(r) for r in (claims.get("roles") or ())),
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
        # Change since the previous signed value of each entered number (unit L7).
        "changes": p.changes,
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


def _logbook_json(logbook, site: str | None = None, roles: Any = None) -> dict[str, Any]:
    """The declaration as a surface renders it; with ``roles``, what this caller may do in it
    (unit W1-A9): ``can_sign`` per log and per entry type, and ``read_only``."""
    out = _declaration_json(logbook, site)
    if roles is not None:
        signs = access_rules.can_sign(logbook, roles)
        out["can_sign"] = signs
        out["read_only"] = not signs
        for t in out["types"]:
            t["can_sign"] = access_rules.can_sign_type(logbook, t["id"], roles)
    return out


def _declaration_json(logbook, site: str | None = None) -> dict[str, Any]:
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
        # The close-out the log declares, or None (unit L5), so a surface offers Close out
        # only where it exists and only to a by_roles holder.
        "seal": None
        if logbook.seal is None
        else {
            "by_roles": list(logbook.seal.by_roles),
            "posture": logbook.seal.posture,
            "fresh_within_seconds": logbook.seal.fresh_within_seconds,
            "boundary": logbook.seal.boundary,
        },
        "readers": list(logbook.readers),
        # The forms the log declares for export (unit L6), so the export panel can offer them.
        "exports": [{"id": e.id, "template": e.template} for e in logbook.exports],
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

    approvals.install()

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

    def roles_at(person: dict[str, Any], site: str | None) -> tuple[str, ...]:
        if not site:
            return ()
        return roles_for(person["handle"], site, state_dir=_state_dir(),
                         gate_roles=person.get("gate_roles", ()))  # fmt: skip

    def may_read(person: dict[str, Any], logbook_id: str, site: str | None) -> bool:
        """The one read check (unit W1-A9): a log the person may not read does not exist."""
        try:
            book = registry.get(logbook_id)
        except LogbookError:
            return True  # an undeclared log: the routes answer as they always did
        return access_rules.can_read(book, roles_at(person, site))

    def _read(person: dict[str, Any], what: str, site: str | None, logbook: str, *, ids=(),
              count=None, params=None) -> None:  # fmt: skip
        """A read of signed records, recorded at the door (reads.py, unit X2). Never raises."""
        reads.record_access(
            state_dir=_state_dir(), principal=person["handle"], what=what, site=site,
            logbook=logbook, ids=ids, count=count, surface="web", params=params,
        )  # fmt: skip

    def _denied(person: dict[str, Any], what: str, site: str | None, logbook: str,
                resp: JSONResponse) -> JSONResponse:  # fmt: skip
        """A refused read, recorded once at the door with who asked, what and why; the answer
        carries no content (#1885). Never raises."""
        try:
            code = json.loads(bytes(resp.body)).get("code")
        except (ValueError, AttributeError):
            code = None
        reads.record_denial(
            state_dir=_state_dir(), principal=person["handle"], what=what, site=site,
            logbook=logbook, code=code, status=resp.status_code, surface="web",
        )  # fmt: skip
        return resp

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
        try:
            site = _site_for(person, None)
        except PermissionError:
            site = None
        roles = roles_at(person, site)
        return {
            "logbooks": [
                _logbook_json(b, None, roles)
                for b in registry.all_logbooks()
                if access_rules.can_read(b, roles)
            ]
        }

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
            book = registry.get(logbook)
        except LogbookError as exc:
            return refuse(404, str(exc), "not_found")
        roles = roles_at(person, the_site or person.get("site"))
        if not access_rules.can_read(book, roles):
            return refuse(404, f"no log {logbook}", "not_found")
        try:
            out = _logbook_json(book, the_site, roles)
        except LogbookError as exc:
            return refuse(404, str(exc), "not_found")
        # Whether this person holds the importer role for the log now (#1804): a page offers
        # Import only then. The commit is still checked at the gate.
        from . import imports
        from .roles import holds_log_role

        out["may_import"] = bool(the_site) and holds_log_role(
            person["handle"], the_site, book.id, imports.IMPORTER_ROLE, state_dir=_state_dir(),
            now=imports._now())[0]  # fmt: skip
        return out

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
                occurred_at=body.get("occurred_at"),
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
            roles = roles_for(person["handle"], ctx["site_id"], state_dir=_state_dir(),
                              gate_roles=person.get("gate_roles", ()))  # fmt: skip
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
            detail = str(exc)
            if "holds no role that may sign" in detail:
                from .roles import GATE_PAIRS_KEY, unpaired

                loose = unpaired(ctx["site_id"], person.get("gate_roles", ()))
                if loose:
                    # Told why and how to fix it (#1800): nothing is inferred from a role's name.
                    detail += (
                        f". Your sign-in's roles {loose} are paired with no signing role at "
                        f"{ctx['site_id']}; a site administrator declares the pair in the site "
                        f"setting {GATE_PAIRS_KEY} (gate role = signing role), or grants a "
                        "signing role with `log role grant`"
                    )
            return refuse(403, detail, code)
        rec = result.record
        out: dict[str, Any] = {"record": rec, "uri": f"axiom://attest/sha256:{rec['digest']}"}
        if result.anchor is not None:
            out["anchor"] = {k: (str(v) if k == "created_at" else v) for k, v in result.anchor.items()}
        return JSONResponse(out, status_code=201)

    # -- approvals (unit X3) --------------------------------------------------------

    def _approver(person: dict[str, Any], site: str) -> bool:
        from axiom.extensions.builtins.attest.logbooks import PLATFORM_ADMIN_ROLES

        held = set(roles_for(person["handle"], site, state_dir=_state_dir())) - set(PLATFORM_ADMIN_ROLES)
        return "approver" in held

    def _held_json(action) -> dict[str, Any]:
        return {
            "action_id": action.action_id,
            "name": action.name,
            "reason": action.reason,
            **service.held_view(action),
            "created_at": action.created_at,
            "requested_by": action.requested_by,
            "decide": approvals.AttestDecisionSigner().card_address(action.action_id),
            # Reject happens at once, at the gate, without the confirm card (held card D1).
            "reject": approvals.reject_address(action.action_id),
        }

    @router.get(subpath + "/approvals/pending", tags=["attest"])
    async def approvals_pending(request: Request, site: str | None = None):
        """The held actions the session's person may decide: all of them for an approver at this
        site, none otherwise. Runs reconcile first, so a decision whose gate step was lost moves."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        g = approvals.gate()
        approvals.reconcile(g)
        held = g.pending()[: approvals.RECONCILE_LIMIT] if _approver(person, the_site) else []
        return {"site_id": the_site, "signed": approvals.flag_on(), "pending": [_held_json(a) for a in held]}

    @router.post(subpath + "/approvals/{action_id}/decision", tags=["attest"])
    async def approvals_decision(request: Request, action_id: str):
        """A draft of the session's person's decision on a held action: ``{decision: approved |
        rejected, reason?}``. The existing present, respond and ``/sign`` (with a gate grant)
        complete it; the signature moves the action."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        body = await body_of(request)
        decision = body.get("decision")
        if decision not in ("approved", "rejected"):
            return refuse(422, "decision is approved or rejected", "decision")
        try:
            the_site = _site_for(person, body.get("site"))
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        action = approvals.gate().get(action_id)
        if action is None:
            return refuse(404, f"no held action {action_id}", "not_found")
        if action.status.value != "pending":
            return refuse(409, f"action {action_id} is already {action.status.value}", "settled")
        try:
            draft_id = service.create_decision_draft(
                site_id=the_site, action=action, approving=decision == "approved",
                reason=str(body.get("reason") or ""), origin="human", for_principal=person["handle"],
            )  # fmt: skip
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return JSONResponse(service.draft(draft_id), status_code=201)

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
        order: str = "seq",
        before: str | None = None,
        after: str | None = None,
    ):
        """One page of a logbook's records, newest page by default (unit W3): a keyset on ``seq``
        (``before_seq`` for older rows, ``after_seq`` for newer), ``limit`` capped at
        ``PAGE_LIMIT`` and applied by the database, every filter a WHERE clause, no count. Rows
        are the brief (id, seq, time, type, signer, title, flags); the record is read on open.
        A page answers 304 to ``If-None-Match`` with its own ETag. With ``unacknowledged=1``,
        only the entries nobody has acknowledged (unit L3); with ``cosign=pending``, only those
        waiting for a co-signature (unit L4). With ``order=occurred`` the page is sorted by when
        each entry happened (an import placed by its own time, #1804) and pages by the cursor
        ``before`` / ``after`` = ``<occurred_at>~<seq>``, given back as ``oldest_key`` and
        ``newest_key``."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return _denied(person, "records", site, logbook, refuse(403, str(exc), "site"))
        if not may_read(person, logbook, the_site):
            return _denied(person, "records", site, logbook, refuse(404, f"no log {logbook}", "not_found"))
        try:
            wanted = filters.parse_filters(
                {"entry_type": entry_type, "meaning": meaning, "signer": signer,
                 "from": from_, "to": to, "interval": interval, "q": q}
            )  # fmt: skip
        except ValueError as exc:
            return refuse(400, str(exc), "filter")
        n = max(1, min(int(limit), service.PAGE_LIMIT))
        if unacknowledged in ("1", "true"):
            rows = service.unacknowledged(the_site, logbook, newest=n)
            has_more = False
        elif cosign == "pending":
            rows = service.pending_cosigns(the_site, logbook, newest=n)
            has_more = False
        elif order == "occurred":
            try:
                before_key, after_key = _cursor(before), _cursor(after)
            except ValueError as exc:
                return refuse(400, str(exc), "filter")
            page = wanted.apply(logbook, the_site, limit=n + 1, order="occurred",
                                before_key=before_key, after_key=after_key)  # fmt: skip
            if after_key is not None:
                has_more, page = len(page) > n, page[:n]
            else:
                has_more, page = len(page) > n, page[-n:]
            rows = service.briefs(page)
        elif order != "seq":
            return refuse(400, "order is seq or occurred", "filter")
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
        if order == "occurred":
            body["page"].update(order="occurred",
                                oldest_key=f"{rows[0]['occurred_at']}~{rows[0]['seq']}" if rows else None,
                                newest_key=f"{rows[-1]['occurred_at']}~{rows[-1]['seq']}" if rows else None)  # fmt: skip
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
            return _denied(person, "intervals", site, logbook, refuse(403, str(exc), "site"))
        if not may_read(person, logbook, the_site):
            return _denied(person, "intervals", site, logbook, refuse(404, f"no log {logbook}", "not_found"))
        listing = intervals.listing(the_site, logbook, kind=kind)
        # The counts are read from the records, so this is a read of the log and is recorded (X2).
        _read(person, "intervals", the_site, logbook, count=len(listing.get("intervals") or []),
              params={"logbook": logbook, "kind": kind})  # fmt: skip
        return {"site_id": the_site, "logbook": logbook, **listing}

    @router.get(subpath + "/{logbook}/records/{attestation_id}", tags=["attest"])
    async def get_record(request: Request, logbook: str, attestation_id: str, site: str | None = None):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        # One refusal per caller at every door (#1886): a session that belongs to another site
        # is told so (403 site) before anything is looked up; a record that is not at the
        # person's own site, or a log they may not read there, does not exist for them (404).
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return _denied(person, "record", site, logbook, refuse(403, str(exc), "site"))
        rec = service.record(attestation_id)
        if rec is None or rec["logbook"] != logbook or rec["site_id"] != the_site:
            return _denied(person, "record", site, logbook, refuse(404, f"no record {attestation_id} in {logbook}", "not_found"))
        if not may_read(person, logbook, rec["site_id"]):
            return _denied(person, "record", site, logbook, refuse(404, f"no record {attestation_id} in {logbook}", "not_found"))
        _read(person, "record", rec["site_id"], logbook, ids=[attestation_id],
              params={"logbook": logbook, "id": attestation_id})  # fmt: skip
        return {
            "record": rec,
            "uri": f"axiom://attest/sha256:{rec['digest']}",
            "thread": service.thread(attestation_id),
            # Change since the previous signed value, read now, after corrections (unit L7).
            "changes": changes.for_record(rec),
            # What this record authorised and what it is about (unit X4).
            "links": service.links(attestation_id, state_dir=_state_dir()),
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

    @router.get(subpath + "/{logbook}/seal/preview", tags=["attest"])
    async def seal_preview(
        request: Request,
        logbook: str,
        boundary: str | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
        site: str | None = None,
    ):
        """What a close-out of this log's period would state, computed and not stored (unit L5)."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            registry.get(logbook)
        except LogbookError as exc:
            return refuse(404, str(exc), "not_found")
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        if not may_read(person, logbook, the_site):
            return refuse(404, f"no log {logbook}", "not_found")
        try:
            value = service.seal_preview(the_site, logbook, boundary=boundary, from_seq=from_seq, to_seq=to_seq)
        except AttestRefused as exc:
            return refuse(422, str(exc))
        return {"site_id": the_site, "logbook": logbook, **value}

    @router.get(subpath + "/{logbook}/seal/due", tags=["attest"])
    async def seal_due(request: Request, logbook: str, site: str | None = None):
        """The log's clock periods that ended and are not closed, with the action that closes
        each (#1806). Only a person who may close the log sees them; anyone else gets none."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        if not may_read(person, logbook, the_site):
            return refuse(404, f"no log {logbook}", "not_found")
        try:
            due = service.unsealed_periods(the_site, logbook, viewer=person["handle"],
                                           state_dir=_state_dir())  # fmt: skip
        except LogbookError as exc:
            return refuse(404, str(exc), "not_found")
        return {"site_id": the_site, "logbook": logbook, "periods": due}

    @router.post(subpath + "/{logbook}/seal", tags=["attest"])
    async def seal_logbook(request: Request, logbook: str):
        """A draft that closes out a period of this log for the session's person (unit L5): their
        summary, and the range, chain head and open items the server computed. The existing
        present, respond and sign routes complete it, and ``/sign`` reports the anchor."""
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            registry.get(logbook)
        except LogbookError as exc:
            return refuse(404, str(exc), "not_found")
        body = await body_of(request)
        try:
            the_site = _site_for(person, body.get("site"))
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        if not may_read(person, logbook, the_site):
            return refuse(404, f"no log {logbook}", "not_found")
        try:
            draft_id = service.create_seal_draft(
                site_id=the_site, logbook=logbook, summary=str(body.get("summary") or ""),
                origin="human", for_principal=person["handle"], boundary=body.get("boundary") or None,
                from_seq=body.get("from_seq"), to_seq=body.get("to_seq"),
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

    # -- imports (#1804): a dry run here; the commit is at the gate, never on this API ----------

    @router.post(subpath + "/{logbook}/imports/dry-run", tags=["attest"])
    async def import_dry_run(request: Request, logbook: str):
        """What importing the history in ``content`` (mapped JSON Lines) would write: counts,
        every mapping decision, warnings and the digest. Writes nothing to the chain. For a person
        holding the importer role for the log, the dry run is kept and ``commit_url`` names the
        page at the node's gate where they commit it with their own sign-in; there is no HTTP
        route that commits an import (ADR-262)."""
        from . import imports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        body = await body_of(request)
        try:
            the_site = _site_for(person, body.get("site"))
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        if not may_read(person, logbook, the_site):
            return refuse(404, f"no log {logbook}", "not_found")
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return refuse(400, "send the mapped history as content, JSON Lines text", "no_content")
        from .roles import holds_log_role

        held, _why = holds_log_role(person["handle"], the_site, logbook, imports.IMPORTER_ROLE,
                                    state_dir=_state_dir(), now=imports._now())  # fmt: skip
        try:
            if held:
                out = imports.prepare(the_site, logbook, content.encode("utf-8"), by=person["handle"],
                                      source=body.get("source"), return_to=body.get("return_to"),
                                      state_dir=_state_dir())  # fmt: skip
                out["commit_url"] = f"/gate/import/commit?prepared_id={out['prepared_id']}"
            else:
                out = imports.dry_run(the_site, logbook, content.encode("utf-8"), source=body.get("source"))
                out.update(prepared_id=None, commit_url=None)
        except AttestRefused as exc:
            return refuse(422, str(exc))
        out["may_commit"] = held
        return JSONResponse(out)

    @router.get(subpath + "/{logbook}/imports", tags=["attest"])
    async def import_batches(request: Request, logbook: str, site: str | None = None):
        """The log's import batches: when, by whom, how many entries, and any withdrawal."""
        from . import imports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")
        if not may_read(person, logbook, the_site):
            return refuse(404, f"no log {logbook}", "not_found")
        return JSONResponse({"site_id": the_site, "logbook": logbook,
                             "batches": imports.batches(the_site, logbook)})  # fmt: skip

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
            return _denied(person, "obligations", site, logbook, refuse(403, str(exc), "site"))
        if not may_read(person, logbook, the_site):
            return _denied(person, "obligations", site, logbook, refuse(404, f"no log {logbook}", "not_found"))
        try:
            states = obligations.evaluate(the_site, logbook, now=datetime.now(UTC))
        except LogbookError as exc:
            return _denied(person, "obligations", site, logbook, refuse(404, str(exc), "not_found"))
        return {
            "site_id": the_site,
            "logbook": logbook,
            "obligations": [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in st.items()}
                for st in states
            ],
            # The misses, with the id an explaining entry names (#1810).
            "misses": obligations.misses(the_site, logbook),
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
            return _denied(person, "verify", site, logbook, refuse(403, str(exc), "site"))
        if not may_read(person, logbook, the_site):
            return _denied(person, "verify", site, logbook, refuse(404, f"no log {logbook}", "not_found"))
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

    # -- exports (unit L6) -----------------------------------------------------------
    # A person who cannot read the log's site gets 404: the export does not exist for them.

    def _export_site(person: dict[str, Any], site: str | None) -> str | JSONResponse:
        """The person's site, or the one refusal every door gives a session that belongs to
        another site (#1886): 403 ``site``. A log they may not read at their own site is 404."""
        try:
            return _site_for(person, site)
        except PermissionError as exc:
            return refuse(403, str(exc), "site")

    @router.post(subpath + "/{logbook}/exports", tags=["attest"])
    async def create_export(request: Request, logbook: str, site: str | None = None):
        """Create an evidence package: the body names ``formats`` (``jsonl``, ``text``, ``csv``),
        ``forms`` (declared ``[[export]]`` ids) and the filters (W3's, plus
        ``include_supplements``). Returns the export's metadata, with its id."""
        from . import exports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        body = await body_of(request)
        the_site = _export_site(person, site or body.get("site"))
        if isinstance(the_site, JSONResponse):
            return _denied(person, "export", site or body.get("site"), logbook, the_site)
        if (
            not may_read(person, logbook, the_site)
            or not filters.RecordFilters().apply(logbook, the_site, limit=1)
        ):
            # Not this person's site, or a log with no chain here: nothing to export, as far as
            # they can tell.
            return _denied(person, "export", site or body.get("site"), logbook, refuse(404, f"no log {logbook}", "not_found"))
        params = {k: v for k, v in body.items() if k not in ("formats", "forms", "site")}
        try:
            f = exports.parse_filter(params)
            row, pkg = exports.create(
                the_site, logbook, f, formats=body.get("formats") or [],
                templates=body.get("forms") or [], requested_by=person["handle"],
            )  # fmt: skip
        except exports.ExportRefused as exc:
            return refuse(400, str(exc), "export")
        except AttestRefused as exc:
            return refuse(400, str(exc), "export")
        _read(person, "export", the_site, logbook, count=pkg.selected,
              params={"logbook": logbook, "export_id": row["export_id"]})  # fmt: skip
        return {**row, "head": pkg.manifest["head"], "counts": pkg.manifest["counts"],
                "files": sorted(pkg.manifest["files"])}  # fmt: skip

    @router.post(subpath + "/{logbook}/exports/preview", tags=["attest"])
    async def preview_export(request: Request, logbook: str, site: str | None = None):
        """How many entries an export of this selection would hold, before it is made (Axiom
        #1970): the body is ``POST /{logbook}/exports``'s (its formats and forms are ignored).
        Returns ``selected``, ``total`` and ``supplements``; writes nothing; refuses as that route
        does."""
        from . import exports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        body = await body_of(request)
        the_site = _export_site(person, site or body.get("site"))
        if isinstance(the_site, JSONResponse):
            return _denied(person, "export", site or body.get("site"), logbook, the_site)
        if (
            not may_read(person, logbook, the_site)
            or not filters.RecordFilters().apply(logbook, the_site, limit=1)
        ):
            return _denied(person, "export", site or body.get("site"), logbook,
                           refuse(404, f"no log {logbook}", "not_found"))  # fmt: skip
        params = {k: v for k, v in body.items() if k not in ("formats", "forms", "site")}
        try:
            out = exports.counted(the_site, logbook, exports.parse_filter(params))
        except (exports.ExportRefused, AttestRefused) as exc:
            return refuse(400, str(exc), "export")
        _read(person, "export", the_site, logbook, count=out["selected"] + out["supplements"],
              params={"logbook": logbook, "preview": True})  # fmt: skip
        return out

    @router.get(subpath + "/{logbook}/exports", tags=["attest"])
    async def list_exports(request: Request, logbook: str, site: str | None = None, limit: int = 50):
        """The exports of a log at the person's site, newest first."""
        from . import exports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        the_site = _export_site(person, site)
        if isinstance(the_site, JSONResponse):
            return _denied(person, "export", site, logbook, the_site)
        if not may_read(person, logbook, the_site):
            return _denied(person, "export", site, logbook, refuse(404, f"no log {logbook}", "not_found"))
        return {"site_id": the_site, "logbook": logbook,
                "exports": exports.listed(the_site, logbook, limit=limit)}  # fmt: skip

    @router.get(subpath + "/{logbook}/forms/{form}/blank", tags=["attest"])
    async def blank_form(request: Request, logbook: str, form: str, site: str | None = None,
                         format: str = "text"):  # noqa: A002 - the query parameter's name
        """A declared form printed blank, with page numbers, for a backup binder (#1805)."""
        from . import exports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        the_site = _export_site(person, site)
        if isinstance(the_site, JSONResponse):
            return the_site
        if not may_read(person, logbook, the_site):
            return refuse(404, f"no log {logbook}", "not_found")
        try:
            book = registry.get(logbook)
            name = exports.template_filename(the_site, book, form)
            if format == "pdf":
                pdf = exports.render_blank_pdf(the_site, book, form)
                stem = name.rsplit(".", 1)[0]
                return Response(pdf, media_type="application/pdf",
                                headers={"Content-Disposition": f'inline; filename="{stem}.pdf"'})  # fmt: skip
            text = exports.render_blank(the_site, book, form)
        except (exports.ExportRefused, LogbookError) as exc:
            return refuse(404, str(exc), "not_found")
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})  # fmt: skip

    @router.get(subpath + "/exports/{export_id}", tags=["attest"])
    async def export_meta(request: Request, export_id: str, site: str | None = None):
        from . import exports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        the_site = _export_site(person, site)
        if isinstance(the_site, JSONResponse):
            return the_site
        row = exports.get(export_id, site=the_site)
        if row is None or not may_read(person, row["logbook"], the_site):
            return refuse(404, f"no export {export_id}", "not_found")
        return row

    @router.get(subpath + "/exports/{export_id}/download", tags=["attest"])
    async def export_download(request: Request, export_id: str, site: str | None = None):
        from . import exports

        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        the_site = _export_site(person, site)
        if isinstance(the_site, JSONResponse):
            return the_site
        row = exports.get(export_id, site=the_site)
        if row is not None and not may_read(person, row["logbook"], the_site):
            row = None
        data = exports.download(export_id, site=the_site) if row else None
        if row is None or data is None:
            return refuse(404, f"no export {export_id}", "not_found")
        _read(person, "export", the_site, row["logbook"],
              params={"logbook": row["logbook"], "export_id": export_id, "download": True})  # fmt: skip
        name = f"evidence-{row['logbook']}-{export_id[:8]}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})  # fmt: skip

    # -- the live stream (ADR-147) ---------------------------------------------------

    @router.get(subpath + "/{logbook}/stream", tags=["attest"])
    async def stream(request: Request, logbook: str, site: str | None = None):
        person = who(request)
        if isinstance(person, JSONResponse):
            return person
        try:
            the_site = _site_for(person, site)
        except PermissionError as exc:
            return _denied(person, "stream", site, logbook, refuse(403, str(exc), "site"))
        if not may_read(person, logbook, the_site):
            return _denied(person, "stream", site, logbook, refuse(404, f"no log {logbook}", "not_found"))
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


def _cursor(value: str | None) -> tuple[datetime, int] | None:
    """``<occurred_at>~<seq>`` to the keyset a page sorted by time reads (#1804)."""
    if not value:
        return None
    when, sep, seq = value.rpartition("~")
    try:
        if not sep:
            raise ValueError
        return datetime.fromisoformat(when.replace("Z", "+00:00")), int(seq)
    except ValueError:
        raise ValueError(f"cursor {value!r} is <occurred_at>~<seq>") from None


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
