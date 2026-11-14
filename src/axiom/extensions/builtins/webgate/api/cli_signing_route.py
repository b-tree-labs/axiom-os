# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/gate/cli/sign`` — the command line signs a log entry with the sign-in the person has here.

A log entry whose floor is ``sso``, or whose type needs a fresh sign-in, cannot be signed at
a prompt: the prompt proves neither (#1669, #1870). The command line hands the person to this
gate instead, the same way ``/gate/cli/authorize`` hands it a key:

- ``GET /gate/cli/sign`` — the page the command line opens (or prints). Signed out, it sends
  the person to the gate's own sign-in and back. Signed in, it shows the entry as presented
  and one button.
- ``POST /gate/cli/sign`` — that button. Mints the signing grant from this session exactly as
  the web confirm card does (:func:`signing._mint_from_session`): bound to the presentation,
  its digest and the person, carrying the sign-in time, short-lived and single use. A sign-in
  older than the type's window goes back through sign-in with ``max_age``. The grant is sealed
  under a one-time code and the browser returns to the listener on 127.0.0.1.
- ``POST /gate/cli/sign/token`` — the command line trades the code and its PKCE verifier for
  the grant. Spent on first use; refused after two minutes.

No identity is decided here that the gate does not already hold, nothing is logged that
carries a code or a grant, and each grant handed to a command line is recorded.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..cli_signin import CodeStore, SignInRequest, SignInRequestError, parse_request, pkce_matches

log = logging.getLogger(__name__)

_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}
_ID_RE = re.compile(r"[0-9a-fA-F-]{36}")
#: The gate's sign-in, and where it asks the identity provider to sign the person in again.
LOGIN_PATH = "/gate/login"
OIDC_LOGIN_PATH = "/gate/oidc/login"


def codes_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir()) / "attest" / "cli_signing_codes.json"


def _seal(code: str, grant: str) -> str:
    from ..cli_signin import _sealer

    return _sealer(code).encrypt(grant.encode("utf-8")).decode("ascii")


def _unseal(code: str, box: str) -> str:
    from ..cli_signin import _sealer

    return _sealer(code).decrypt(box.encode("ascii")).decode("utf-8")


def _parse(params: dict[str, Any]) -> tuple[SignInRequest, str]:
    req = parse_request(params)
    presentation_id = str(params.get("presentation_id") or "")
    if not _ID_RE.fullmatch(presentation_id):
        raise SignInRequestError("the request's presentation is missing or malformed")
    return req, presentation_id


def _here(req: SignInRequest, presentation_id: str) -> str:
    return "/gate/cli/sign?" + urlencode(
        {"redirect_uri": req.redirect_uri, "state": req.state, "code_challenge": req.code_challenge,
         "code_challenge_method": "S256", "presentation_id": presentation_id}
    )  # fmt: skip


def register_cli_signing_routes(
    router: APIRouter,
    *,
    issuer_of: Callable[[Request], str],
    mint: Callable[..., tuple[str | None, dict[str, Any] | None]],
    store_path: Callable[[], Path] = codes_path,
) -> None:
    from axiom.webauth import session_from_cookies

    @router.get("/gate/cli/sign")
    async def sign_page(request: Request) -> Response:
        try:
            req, presentation_id = _parse(dict(request.query_params))
        except SignInRequestError as exc:
            return _page("This link cannot be used", f"<p>{html.escape(str(exc))}</p>", 400)
        claims = session_from_cookies(request.cookies, issuer=issuer_of(request))
        if not claims:
            nxt = quote(_here(req, presentation_id), safe="")
            return RedirectResponse(f"{LOGIN_PATH}?next={nxt}", status_code=303, headers=_NO_STORE)
        return _confirm_page(req, presentation_id, claims)

    @router.post("/gate/cli/sign")
    async def sign_approve(request: Request) -> Response:
        form = dict(await request.form())
        try:
            req, presentation_id = _parse(form)
        except SignInRequestError as exc:
            return _page("This link cannot be used", f"<p>{html.escape(str(exc))}</p>", 400)
        token, refusal = mint(request, issuer_of(request), presentation_id)
        if refusal is not None:
            body = refusal["body"]
            here = quote(_here(req, presentation_id), safe="")
            if body.get("code") == "no_session":
                return RedirectResponse(f"{LOGIN_PATH}?next={here}", status_code=303, headers=_NO_STORE)
            if body.get("code") == "reauth_required" and body.get("max_age") is not None:
                # Signed in too long ago for this entry: sign in again, then come back here.
                return RedirectResponse(f"{OIDC_LOGIN_PATH}?max_age={int(body['max_age'])}&next={here}",
                                        status_code=303, headers=_NO_STORE)  # fmt: skip
            return _back(req, {"error": "access_denied", "error_description": str(body.get("detail"))})
        store = CodeStore(store_path())
        code = store._issue(req, lambda c: {"kind": "signing", "presentation_id": presentation_id,
                                            "sealed": _seal(c, token)})  # fmt: skip
        _record(request, issuer_of(request), presentation_id)
        return _back(req, {"code": code})

    @router.post("/gate/cli/sign/token")
    async def sign_token(request: Request) -> JSONResponse:
        try:
            body: Any = await request.json()
        except Exception:  # noqa: BLE001 - an unauthenticated route receives junk
            body = None
        if not isinstance(body, dict):
            return _refuse(400, "send a JSON object with code, code_verifier and redirect_uri")
        code = str(body.get("code") or "")
        entry = CodeStore(store_path()).consume(code)
        if entry is None or entry.get("kind") != "signing":
            return _refuse(400, "this code is unknown, used or expired; open the link again")
        if str(body.get("redirect_uri") or "") != entry.get("redirect_uri"):
            return _refuse(400, "this code was issued to another return address")
        if not pkce_matches(str(body.get("code_verifier") or ""), str(entry.get("code_challenge"))):
            return _refuse(400, "the code verifier does not match; open the link again")
        try:
            grant = _unseal(code, str(entry.get("sealed") or ""))
        except Exception:  # noqa: BLE001 - a box that cannot be opened is a refusal
            return _refuse(400, "this code cannot be opened; open the link again")
        return JSONResponse({"grant": grant, "presentation_id": entry.get("presentation_id")},
                            headers=_NO_STORE)  # fmt: skip


def _record(request: Request, issuer: str, presentation_id: str) -> None:
    """One record on the action audit chain per grant handed to a command line: who, for which
    presentation. Never the grant or the code. Never fails the hand-off."""
    try:
        from axiom.extensions.builtins.attest.naming import signer_handle
        from axiom.infra import audit_trail
        from axiom.infra.paths import get_user_state_dir
        from axiom.webauth import session_from_cookies

        claims = session_from_cookies(request.cookies, issuer=issuer) or {}
        audit_trail.record_invocation(
            state_dir=Path(get_user_state_dir()), skill="attest.cli_signing_grant",
            params={"presentation_id": presentation_id}, principal=signer_handle(claims),
            site=claims.get("site"), outcome="ok", errors_count=0,
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.warning("cli signing: the hand-off for %s was not recorded", presentation_id)


def _back(req: SignInRequest, query: dict[str, str]) -> RedirectResponse:
    """To the listener waiting on this computer; its address was proven to be the loopback."""
    q = urlencode({**query, "state": req.state})
    return RedirectResponse(f"{req.redirect_uri}?{q}", status_code=303, headers=_NO_STORE)


def _refuse(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": "invalid_grant"}, status_code=status,
                        headers=_NO_STORE)  # fmt: skip


def _confirm_page(req: SignInRequest, presentation_id: str, claims: dict[str, Any]) -> HTMLResponse:
    from axiom.extensions.builtins.attest import service
    from axiom.extensions.builtins.attest.naming import signer_handle

    try:
        ctx = service.presentation_context(presentation_id)
        shown = service.presentation_text(presentation_id)
    except Exception:  # noqa: BLE001 - an unknown presentation is a refusal, not a crash
        return _page("Nothing to sign", "<p>This entry is no longer waiting to be signed.</p>", 404)
    try:
        who = signer_handle(claims)
    except Exception:  # noqa: BLE001
        who = ""
    fields = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
        for k, v in (("redirect_uri", req.redirect_uri), ("state", req.state),
                     ("code_challenge", req.code_challenge), ("code_challenge_method", "S256"),
                     ("presentation_id", presentation_id))
    )  # fmt: skip
    inner = (
        f"<p>Your command line asks you to sign this entry in <b>{html.escape(ctx['logbook'])}</b>"
        f" as <b>{html.escape(who)}</b>.</p><pre>{html.escape(shown)}</pre>"
        f'<form method="post" action="/gate/cli/sign">{fields}'
        '<button type="submit">Sign this entry</button></form>'
        "<p>Close this page to leave it unsigned. The draft stays in your tray.</p>"
    )
    return _page("Sign from your command line", inner, 200)


def _page(title: str, inner: str, status: int) -> HTMLResponse:
    body = (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<meta name='viewport' content='width=device-width'></head><body><main>"
            f"<h1>{html.escape(title)}</h1>{inner}</main></body></html>")  # fmt: skip
    return HTMLResponse(body, status_code=status,
                        headers={**_NO_STORE, "x-frame-options": "DENY",
                                 "content-security-policy": "frame-ancestors 'none'"})  # fmt: skip


__all__ = ["codes_path", "register_cli_signing_routes"]
