# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/gate/cli/*`` — the command line signs in through the browser and never shows a key.

Four routes, in the order a sign-in uses them:

- ``GET /gate/cli/authorize`` — the page the command line opens. Signed out, it offers
  the site's sign-in buttons and an invitation-code box. Signed in, it says who you
  are, what a key from your role may do, and asks for one click.
- ``POST /gate/cli/approve`` — that click. Decides the key by the same rules as the
  Keys page (:func:`self_service_decision`), stores a one-time code and sends the
  browser back to the listener on 127.0.0.1.
- ``POST /gate/cli/invitation`` — an invitation code typed into the page. Checked
  (not spent) here, so a wrong or used code is refused where it was typed.
- ``POST /gate/cli/token`` — the command line trades the one-time code and its PKCE
  verifier for the key. The key is minted here, once, and the reply is not cached.

The rules that make this safe (loopback-only return address, PKCE S256, single use,
a two-minute code, nothing secret at rest) live in :mod:`..cli_signin` with their
reasons. What this module owns is the HTTP and the pages. Nothing here logs a code,
an invitation or a key.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..cli_signin import (
    CodeStore,
    SignInRequest,
    SignInRequestError,
    parse_request,
    pkce_matches,
    store_path_for,
    unseal_invitation,
)
from .self_key_route import (
    Refusal,
    SelfServiceGrant,
    _plain,
    allowed_sites,
    mint_self_service_key,
    self_service_decision,
)

log = logging.getLogger(__name__)

_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}

#: Never inside another site's frame: the Connect button must not be clickable
#: through somebody else's page.
_NO_FRAME = {"x-frame-options": "DENY", "content-security-policy": "frame-ancestors 'none'"}

Shell = Callable[..., HTMLResponse]


def _guarded(page: HTMLResponse) -> HTMLResponse:
    for k, v in {**_NO_STORE, **_NO_FRAME}.items():
        page.headers[k] = v
    return page


def register_cli_signin_routes(
    router: APIRouter,
    *,
    keys_path: Callable[[], Path | None],
    invitations_path: Callable[[], Path | None],
    claims_of: Callable[[Request], dict | None],
    shell: Shell,
    providers: Callable[[], list] = lambda: [],
    now: Callable[[], float] | None = None,
) -> None:
    """Attach the four routes. Every seam is a callable, read per request."""

    def _store() -> CodeStore | None:
        path = store_path_for(keys_path())
        if path is None:
            return None
        return CodeStore(path, now=now) if now is not None else CodeStore(path)

    @router.get("/gate/cli/authorize")
    async def authorize(request: Request) -> Response:
        try:
            req = parse_request(dict(request.query_params))
        except SignInRequestError as exc:
            return _bad_request(shell, str(exc))
        claims = claims_of(request)
        if not claims:
            return _sign_in_page(shell, req, request, providers())
        decision = self_service_decision(claims, req.site)
        if isinstance(decision, Refusal):
            return _back_with_error(req, decision.detail)
        return _consent_page(shell, req, decision, claims)

    @router.post("/gate/cli/approve")
    async def approve(request: Request) -> Response:
        form = dict(await request.form())
        try:
            req = parse_request(form)
        except SignInRequestError as exc:
            return _bad_request(shell, str(exc))
        decision = self_service_decision(claims_of(request), req.site)
        if isinstance(decision, Refusal):
            if decision.code == "no_session":
                return _sign_in_page(shell, req, request, providers())
            return _back_with_error(req, decision.detail)
        store = _store()
        if store is None:
            return _back_with_error(req, "this node does not serve API keys")
        code = store.issue_for_session(
            req,
            {
                "handle": decision.handle,
                "roles": list(decision.roles),
                "scopes": list(decision.scopes),
                "site": decision.site,
            },
        )
        return _back_with_code(req, code)

    @router.post("/gate/cli/invitation")
    async def invitation(request: Request) -> Response:
        from axiom.webauth.invitations import (
            InvitationRefused,
            InvitationsFileError,
            check_invitation,
        )

        from .redeem_route import strict_role_resolver

        form = dict(await request.form())
        try:
            req = parse_request(form)
        except SignInRequestError as exc:
            return _bad_request(shell, str(exc))
        typed = str(form.get("invitation_code") or "").strip()
        if not typed:
            return _sign_in_page(shell, req, request, providers(), error="Enter your invitation code.")
        invites, store = invitations_path(), _store()
        if invites is None or store is None:
            return _back_with_error(req, "this node does not serve invitation redemption")
        try:
            check_invitation(invites, typed, resolve_roles=strict_role_resolver())
        except InvitationRefused as exc:
            # Refused on the page where it was typed: the person can correct a
            # paste without starting again. The code itself is never echoed.
            return _sign_in_page(shell, req, request, providers(), error=str(exc))
        except InvitationsFileError:
            log.error("cli sign-in: invitations file unreadable")
            return _back_with_error(req, "this node cannot read its invitations")
        return _back_with_code(req, store.issue_for_invitation(req, typed))

    @router.post("/gate/cli/token")
    async def token(request: Request) -> JSONResponse:
        try:
            body: Any = await request.json()
        except Exception:  # noqa: BLE001 - an unauthenticated route receives junk
            body = None
        if not isinstance(body, dict):
            return _refuse(400, "send a JSON object with code, code_verifier and redirect_uri",
                           "bad_request")
        code = str(body.get("code") or "")
        store = _store()
        if store is None:
            return _refuse(503, "this node does not serve API keys", "unavailable")
        entry = store.consume(code)
        # One refusal for unknown, used and expired, as an OAuth server gives:
        # the holder of a real code that failed learns enough (sign in again),
        # and a guesser learns nothing about which codes ever existed.
        if entry is None:
            return _refuse(400, "this sign-in code is unknown, used or expired; sign in again",
                           "invalid_grant")
        if str(body.get("redirect_uri") or "") != entry.get("redirect_uri"):
            return _refuse(400, "this sign-in code was issued to another return address",
                           "invalid_grant")
        if not pkce_matches(str(body.get("code_verifier") or ""), str(entry.get("code_challenge"))):
            return _refuse(400, "the code verifier does not match; sign in again", "invalid_grant")

        if entry.get("kind") == "session":
            g = entry.get("grant") or {}
            grant = SelfServiceGrant(
                handle=str(g.get("handle") or ""),
                roles=tuple(g.get("roles") or ()),
                scopes=tuple(g.get("scopes") or ()),
                site=str(g.get("site") or ""),
            )
            out = mint_self_service_key(keys_path(), grant)
        elif entry.get("kind") == "invitation":
            from .redeem_route import redeem_and_store

            try:
                typed = unseal_invitation(entry, code)
            except Exception:  # noqa: BLE001 - a store that cannot be opened is a refusal
                return _refuse(400, "this sign-in code cannot be opened; sign in again",
                               "invalid_grant")
            out = redeem_and_store(invitations_path(), keys_path(), typed)
        else:
            return _refuse(400, "this sign-in code is not one this node issued", "invalid_grant")
        if isinstance(out, Refusal):
            return _refuse(out.status, out.detail, out.code)
        log.info("cli sign-in: key %s issued to %s", out.get("key_id"), out.get("principal"))
        return JSONResponse(out, headers=_NO_STORE)


# --------------------------------------------------------------------------- redirects


def _back_with_code(req: SignInRequest, code: str) -> RedirectResponse:
    query = urlencode({"code": code, "state": req.state})
    return RedirectResponse(f"{req.redirect_uri}?{query}", status_code=303, headers=_NO_STORE)


def _back_with_error(req: SignInRequest, detail: str) -> RedirectResponse:
    """Send the reason to the waiting command line, so it says it in the terminal.

    Safe to redirect: the address was already proven to be this computer's loopback.
    """
    query = urlencode({"error": "access_denied", "error_description": detail, "state": req.state})
    return RedirectResponse(f"{req.redirect_uri}?{query}", status_code=303, headers=_NO_STORE)


def _refuse(status: int, detail: str, code: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status, headers=_NO_STORE)


# --------------------------------------------------------------------------- pages


def _hidden(req: SignInRequest) -> str:
    fields = {
        "redirect_uri": req.redirect_uri,
        "state": req.state,
        "code_challenge": req.code_challenge,
        "code_challenge_method": "S256",
    }
    return "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
        for k, v in fields.items()
    )


def _bad_request(shell: Shell, problem: str) -> HTMLResponse:
    inner = (
        '<h2 class="title">This sign-in link is not valid</h2>'
        f'<p class="help">{html.escape(problem)}.</p>'
        '<p class="help">Start again from the command line on your computer.</p>'
    )
    return _guarded(shell(title="Sign-in link not valid", inner=inner, status=400))


def _sign_in_page(
    shell: Shell, req: SignInRequest, request: Request, providers: list, *, error: str = ""
) -> HTMLResponse:
    from .routers import _GLYPHS

    here = request.url.path + "?" + urlencode(
        {
            "redirect_uri": req.redirect_uri,
            "state": req.state,
            "code_challenge": req.code_challenge,
            "code_challenge_method": "S256",
            **({"site": req.site} if req.site else {}),
        }
    )
    nxt = quote(here, safe="")
    buttons = "".join(
        f'<a class="sso" href="{html.escape(p.login_path + "?next=" + nxt, quote=True)}">'
        f'{_GLYPHS.get(getattr(p, "kind", "oidc"), _GLYPHS["oidc"])}'
        f"<span>{html.escape(p.label)}</span></a>"
        for p in providers
    )
    password = (
        f'<p class="alt"><a href="{html.escape("/gate/login?next=" + nxt, quote=True)}">'
        "Sign in with a password instead</a></p>"
    )
    err = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    inner = f"""<h2 class="title">Connect your command line</h2>
<p class="help">Sign in, and the command line waiting on this computer receives its own
key. You will not need to copy anything.</p>
{err}<div class="sso-list">{buttons}</div>{password if not buttons else ""}
<div class="or">or use an invitation code</div>
<form method="post" action="/gate/cli/invitation" autocomplete="off">
  {_hidden(req)}
  <div class="field"><label for="invitation_code">Invitation code</label>
  <input id="invitation_code" name="invitation_code" type="password" required
         placeholder="axi_inv_..." autocomplete="off"></div>
  <button type="submit" class="submit">Use invitation</button>
</form>{password if buttons else ""}"""
    return _guarded(shell(title="Connect your command line", inner=inner, status=401 if error else 200))


def _consent_page(
    shell: Shell, req: SignInRequest, grant: SelfServiceGrant, claims: dict
) -> HTMLResponse:
    from ..role_bundles import default_bundle_registry

    sites = allowed_sites(claims, default_bundle_registry(), grant.roles)
    if len(sites) > 1:
        options = "".join(
            f'<option value="{html.escape(s, quote=True)}"{" selected" if s == grant.site else ""}>'
            f"{html.escape(s)}</option>"
            for s in sites
        )
        site_field = (
            f'<div class="field"><label for="site">For site</label>'
            f'<select id="site" name="site">{options}</select></div>'
        )
    else:
        site_field = (
            f'<input type="hidden" name="site" value="{html.escape(grant.site, quote=True)}">'
            f'<p class="help">For site <strong>{html.escape(grant.site)}</strong></p>'
        )
    allows = "".join(f"<li>{html.escape(_plain(s))}</li>" for s in grant.scopes)
    inner = f"""<h2 class="title">Connect your command line?</h2>
<p class="help">Signed in as <strong>{html.escape(grant.handle)}</strong></p>
<p>The command line waiting on this computer will get its own key, stored in your vault
and never shown. It will be able to:</p><ul>{allows}</ul>
<form method="post" action="/gate/cli/approve">
  {_hidden(req)}{site_field}
  <button type="submit" class="submit">Connect</button>
</form>
<p class="alt"><a href="{html.escape(_cancel_href(req), quote=True)}">Cancel</a></p>"""
    return _guarded(shell(title="Connect your command line", inner=inner))


def _cancel_href(req: SignInRequest) -> str:
    query = urlencode(
        {"error": "access_denied", "error_description": "you cancelled the sign-in", "state": req.state}
    )
    return f"{req.redirect_uri}?{query}"


__all__ = ["register_cli_signin_routes"]
