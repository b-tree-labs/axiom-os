# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``POST /gate/approvals/{action_id}/reject`` — a person rejects a held action at once.

The held-action card (appkit design 2026-11-13, decision D1) rejects without the confirm card:
rejecting is safe to undo (the agent may ask again; DC-10), so it is one press. It is still the
person's signed decision, made by the same steps the confirm card's path takes, here in one call:
draft the rejection (``service.create_decision_draft``), present it, record the answer ``sign``,
mint the grant from the gate session and the device cookie (``_mint_from_session``, ADR-146) and
sign. The signature moves the action (``approvals._after_sign``), so there is one approvals chain
and one way an action is decided.

Only the gate session reaches it: the person is read from the session cookie, never a key
(ADR-262). A refusal at any step discards the draft, so nothing is left half done:

- 401 ``no_session`` signed out; 404 ``not_found`` no such action; 409 ``settled`` decided already;
- 403 with the grant's ``code`` (``reauth_required`` carries ``max_age``) or the signature's
  (a person who holds no ``approver`` role).

Answers 201 ``{decision, decided_by, recorded_at, record}``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from axiom.extensions.builtins.attest.approvals import REJECT_PATH


def _refuse(status: int, detail: str, code: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code, **extra}, status_code=status,
                        headers={"cache-control": "no-store"})  # fmt: skip


def register_approval_reject_route(
    router: APIRouter, *, issuer_of: Callable[[Request], str], mint: Callable[..., Any]
) -> None:
    @router.post(REJECT_PATH)
    async def reject(request: Request, action_id: str) -> Response:
        from axiom.extensions.builtins.attest import approvals, service, signing
        from axiom.extensions.builtins.attest.api import _signatory, _site_for
        from axiom.extensions.builtins.attest.naming import SignerNameError, signer_handle, signer_site
        from axiom.extensions.builtins.attest.roles import roles_for
        from axiom.extensions.builtins.attest.service import AttestRefused
        from axiom.infra.paths import get_user_state_dir
        from axiom.webauth import session_from_cookies

        claims = session_from_cookies(request.cookies, issuer=issuer_of(request))
        if not claims or not claims.get("sub"):
            return _refuse(401, "sign in first", "no_session")
        try:
            handle = signer_handle(claims)
        except SignerNameError as exc:
            return _refuse(403, str(exc), "signer_name")
        except ValueError:
            return _refuse(401, "this sign-in cannot be named", "no_session")
        idp = claims.get("idp")
        person = {
            "handle": handle,
            "site": signer_site(claims) or None,
            "idp": idp,
            "posture": "sso" if idp else "attested",
            "display": str(claims.get("name") or ""),
            "gate_roles": tuple(str(r) for r in (claims.get("roles") or ())),
        }
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        try:
            site = _site_for(person, body.get("site"))
        except PermissionError as exc:
            return _refuse(403, str(exc), "site")
        action = approvals.gate().get(action_id)
        if action is None:
            return _refuse(404, f"no held action {action_id}", "not_found")
        if action.status.value != "pending":
            return _refuse(409, f"action {action_id} is already {action.status.value}", "settled")

        sig = _signatory(person, roles_for(handle, site, state_dir=get_user_state_dir(),
                                           gate_roles=person["gate_roles"]))  # fmt: skip
        try:
            draft_id = service.create_decision_draft(
                site_id=site, action=action, approving=False, reason=str(body.get("reason") or ""),
                origin="human", for_principal=handle,
            )  # fmt: skip
        except AttestRefused as exc:
            return _refuse(422, str(exc), "refused")

        def undo(resp: JSONResponse) -> JSONResponse:
            try:
                service.discard(draft_id, by=handle)
            except AttestRefused:
                pass  # already spent or gone: nothing is left open
            return resp

        try:
            pres = service.present(draft_id, modality="screen")
            service.respond(pres.presentation_id, principal=sig.principal, answer="sign", via="screen")
        except AttestRefused as exc:
            return undo(_refuse(422, str(exc), "refused"))
        token, refusal = mint(request, issuer_of(request), pres.presentation_id,
                              console_id=body.get("console_id"))  # fmt: skip
        if refusal is not None:
            payload = refusal["body"]
            return undo(_refuse(refusal["status"], str(payload.get("detail") or "not signed"),
                                str(payload.get("code") or "refused"),
                                **({"max_age": payload["max_age"]} if "max_age" in payload else {})))  # fmt: skip
        try:
            result = service.sign(pres.presentation_id, signatory=sig, signer=signing.signer(),
                                  source="screen", grant=str(token), grant_keys=signing.public_keys())  # fmt: skip
        except AttestRefused as exc:
            return undo(_refuse(403, str(exc), str(getattr(exc, "code", "refused") or "refused")))
        rec = result.record
        return JSONResponse(
            {"decision": "rejected", "decided_by": handle, "recorded_at": rec.get("recorded_at"), "record": rec},
            status_code=201, headers={"cache-control": "no-store"},
        )  # fmt: skip


__all__ = ["REJECT_PATH", "register_approval_reject_route"]
