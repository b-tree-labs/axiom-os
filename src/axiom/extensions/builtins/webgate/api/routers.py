# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The webgate HTTP surface — a UI-agnostic forward-auth gate (ADR-003).

Routes, all public (the gate IS the authenticator):

- ``GET  /gate/login``  — the password form (Axiom's default web auth UI).
- ``POST /gate/login``  — authenticate, set the session cookie, redirect.
- ``GET  /gate/forgot`` — password-help page (no email reset flow yet).
- ``GET  /gate/verify`` — the forward-auth check an edge proxy subrequests:
  200 + ``X-Axiom-User-*`` headers when a valid session is present, else 401.
- ``GET|POST /gate/logout`` — clear the session (GET so a browser navigation /
  a consumer UI's post-signout redirect can end the gate session in one hop).

The session cookie is a ``webauth`` ES256 token, so the OIDC fast-follow reuses
it verbatim: ``oauth``'s ``SubjectResolver`` reads the same cookie via
``session_from_cookies`` and an already-logged-in browser sails through
``/oauth/authorize`` — one login for cookie-session and OIDC alike.

The login UI is Axiom's default: theme-aware (light/dark), responsive, fully
self-contained (no external fonts/CDN — inline CSS/JS), and **brand-neutral** —
consumers pass a :class:`LoginBrand` to skin it (product name, accent, logo,
forgot/sign-up links). No consumer colour is baked into the platform default.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from axiom.webauth import (
    SESSION_COOKIE,
    User,
    UserStore,
    authenticate,
    get_user_store,
    issue_session_token,
    session_from_cookies,
)
from axiom.webauth.session import DEFAULT_SESSION_TTL

# "Remember me" extends the session well beyond the default browser-session TTL.
REMEMBER_TTL = timedelta(days=30)

# Accent is injected into <style>; constrain it to a colour literal so config can
# never smuggle CSS (defence in depth — it is dev config, not user input).
_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$|^[a-zA-Z]{3,20}$")
_DEFAULT_ACCENT = "#bf5700"  # Axiom's default brand accent (UT burnt orange); per-consumer overridable


@dataclass(frozen=True)
class LoginBrand:
    """How to skin Axiom's default login UI. All fields optional; the defaults
    ship Axiom's own look (UT burnt-orange accent) so the platform is styled out
    of the box, and each consumer overrides only what it needs."""

    product_name: str = "Axiom"
    tagline: str = "Sign in to continue"
    accent: str = _DEFAULT_ACCENT
    logo: str = ""  # inline SVG or an emoji; defaults to the product initial
    forgot_url: str = ""  # where "Forgot password?" links (default: /gate/forgot)
    signup_url: str = ""  # if set, show a "Create account" link
    footer: str = "Protected by Axiom"  # subtle footer; "" hides it

    def safe_accent(self) -> str:
        return self.accent if _ACCENT_RE.match(self.accent or "") else _DEFAULT_ACCENT

    def mark(self) -> str:
        # A visual anchor at the top of the card. Custom logo wins; otherwise the
        # product's initial in an accent-tinted tile.
        if self.logo:
            return self.logo
        return html.escape((self.product_name or "A").strip()[:1].upper())


def _issuer(request: Request) -> str:
    override = os.getenv("AXIOM_GATE_ISSUER") or os.getenv("OAUTH_ISSUER")
    if override:
        return override.rstrip("/")
    return str(request.base_url).rstrip("/")


def _default_secure() -> bool:
    # Secure cookies by default; opt out only for a local http bring-up.
    return os.getenv("AXIOM_GATE_INSECURE_COOKIE", "").lower() not in {"1", "true", "yes"}


def _safe_next(target: str | None) -> str:
    """Only permit a local path — never an absolute or protocol-relative URL.

    Prevents the login form's ``next`` from being turned into an open redirect.
    """
    if not target or not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target


class _SessionLogin(BaseModel):
    """JSON login body for the XHR seam (the Vite UI). Field names mirror what a
    React auth client sends; ``remember`` extends the session like the form."""

    email: str
    password: str
    remember: bool = False


def _user_public(claims_or_user: dict | User) -> dict:
    """The identity a browser UI reads after login. Includes both ``roles`` (the
    Axiom shape) and ``role`` (first role) so a SoilMetrix-style client that reads
    ``role`` converges without changes."""
    if isinstance(claims_or_user, User):
        sub, email, name = claims_or_user.user_id, claims_or_user.email, claims_or_user.name
        roles = list(claims_or_user.roles)
    else:
        sub = str(claims_or_user.get("sub", ""))
        email = str(claims_or_user.get("email", ""))
        name = str(claims_or_user.get("name", ""))
        roles = list(claims_or_user.get("roles", []) or [])
    return {"sub": sub, "email": email, "name": name, "roles": roles,
            "role": roles[0] if roles else ""}


_CSS = """
:root{
  --accent:%%ACCENT%%;
  --bg:#eef0f3; --card:#fff; --text:#1a1d21; --muted:#6b7280; --border:#dcdfe4;
  --input:#fff; --ring:color-mix(in srgb,var(--accent) 30%,transparent);
  --err-bg:#fdeceb; --err-bd:#f3c4c0; --err-tx:#b42318;
  --shadow:0 12px 34px rgba(16,24,40,.10),0 1px 3px rgba(16,24,40,.06);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d1017; --card:#161a21; --text:#e7e9ec; --muted:#98a1ad; --border:#2a303a;
  --input:#0f131a; --err-bg:#3a1d1d; --err-bd:#6b2b2b; --err-tx:#f5b5b1;
  --shadow:0 12px 34px rgba(0,0,0,.5),0 1px 3px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;padding:24px;display:flex;align-items:center;justify-content:center;
  background:var(--bg);color:var(--text);line-height:1.5;-webkit-font-smoothing:antialiased;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.card{background:var(--card);width:100%;max-width:400px;border:1px solid var(--border);
  border-radius:16px;box-shadow:var(--shadow);padding:40px 36px}
.brand{display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:26px}
.mark{width:46px;height:46px;border-radius:13px;display:flex;align-items:center;justify-content:center;
  background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent);
  font-size:22px;font-weight:700;margin-bottom:14px}
.title{font-size:20px;font-weight:640;margin:0}
.tagline{font-size:14px;color:var(--muted);margin:4px 0 0}
form{display:flex;flex-direction:column;gap:15px}
.field{display:flex;flex-direction:column;gap:6px}
label{font-size:13px;font-weight:560;color:var(--muted)}
.pw{position:relative;display:flex;align-items:center}
input[type=email],input[type=password],input[type=text]{
  width:100%;height:44px;padding:0 13px;font-size:16px;color:var(--text);background:var(--input);
  border:1px solid var(--border);border-radius:10px;outline:none;
  transition:border-color .15s,box-shadow .15s}
input::placeholder{color:var(--muted);opacity:.7}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--ring)}
.pw input{padding-right:66px}
.toggle{position:absolute;right:5px;height:32px;padding:0 11px;border:0;background:transparent;
  color:var(--muted);font-size:12px;font-weight:650;cursor:pointer;border-radius:7px}
.toggle:hover{color:var(--text)}
.row{display:flex;align-items:center;justify-content:space-between;font-size:13px;margin-top:1px}
.remember{display:flex;align-items:center;gap:8px;color:var(--muted);cursor:pointer;user-select:none}
.remember input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
a{color:var(--accent);text-decoration:none;font-weight:560}
a:hover{text-decoration:underline}
.submit{height:46px;margin-top:5px;border:0;border-radius:10px;background:var(--accent);color:#fff;
  font-size:15px;font-weight:620;cursor:pointer;transition:filter .15s}
.submit:hover{filter:brightness(1.07)}
.submit:active{filter:brightness(.94)}
.error{background:var(--err-bg);border:1px solid var(--err-bd);color:var(--err-tx);
  font-size:13px;padding:10px 12px;border-radius:10px;margin-bottom:16px}
.alt{text-align:center;font-size:13px;color:var(--muted);margin:18px 0 0}
.footer{text-align:center;font-size:12px;color:var(--muted);margin-top:22px}
.help{font-size:14px;color:var(--muted);text-align:center;margin:0}
@media (max-width:440px){body{padding:16px}.card{padding:30px 22px;border-radius:14px}}
"""


def _shell(brand: LoginBrand, *, title: str, inner: str, status: int = 200) -> HTMLResponse:
    css = _CSS.replace("%%ACCENT%%", brand.safe_accent())
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{css}</style></head><body>"
        f'<main class="card">{inner}</main></body></html>'
    )
    return HTMLResponse(doc, status_code=status)


def _brand_header(brand: LoginBrand) -> str:
    name = html.escape(brand.product_name)
    tag = f'<p class="tagline">{html.escape(brand.tagline)}</p>' if brand.tagline else ""
    return (
        '<div class="brand">'
        f'<div class="mark">{brand.mark()}</div>'
        f'<h1 class="title">{name}</h1>{tag}</div>'
    )


def _login_page(next_target: str, brand: LoginBrand, *, error: str | None = None) -> HTMLResponse:
    safe = html.escape(_safe_next(next_target), quote=True)
    err = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    forgot = html.escape(brand.forgot_url or "/gate/forgot", quote=True)
    signup = (
        f'<p class="alt">New here? <a href="{html.escape(brand.signup_url, quote=True)}">'
        "Create an account</a></p>"
        if brand.signup_url
        else ""
    )
    footer = f'<p class="footer">{html.escape(brand.footer)}</p>' if brand.footer else ""
    inner = f"""{_brand_header(brand)}{err}
<form method="post" action="/gate/login" autocomplete="on">
  <div class="field">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="username"
           autofocus required placeholder="you@example.org">
  </div>
  <div class="field">
    <label for="password">Password</label>
    <div class="pw">
      <input id="password" name="password" type="password"
             autocomplete="current-password" required placeholder="Your password">
      <button type="button" class="toggle" id="pwtoggle" aria-label="Show password">Show</button>
    </div>
  </div>
  <div class="row">
    <label class="remember"><input type="checkbox" name="remember" value="1">Remember me</label>
    <a href="{forgot}">Forgot password?</a>
  </div>
  <input type="hidden" name="next" value="{safe}">
  <button type="submit" class="submit">Sign in</button>
</form>{signup}{footer}
<script>
(function(){{var b=document.getElementById('pwtoggle'),p=document.getElementById('password');
if(b&&p)b.addEventListener('click',function(){{var s=p.type==='password';p.type=s?'text':'password';
b.textContent=s?'Hide':'Show';b.setAttribute('aria-label',(s?'Hide':'Show')+' password');p.focus();}});}})();
</script>"""
    return _shell(brand, title=f"Sign in · {brand.product_name}", inner=inner,
                  status=401 if error else 200)


def _forgot_page(brand: LoginBrand) -> HTMLResponse:
    # No self-service email reset yet — be honest and point at the admin. A
    # consumer with a real reset flow sets LoginBrand.forgot_url to bypass this.
    inner = (
        f"{_brand_header(brand)}"
        '<p class="help">Password resets are handled by your administrator. '
        "Reach out to them to restore access.</p>"
        '<p class="alt"><a href="/gate/login">← Back to sign in</a></p>'
    )
    return _shell(brand, title=f"Password help · {brand.product_name}", inner=inner)


def _spa_brand_payload(brand: LoginBrand) -> dict:
    """The runtime brand the built SPA reads from ``window.__AXIOM_GATE_BRAND__``.

    Field names are the frontend's camelCase (``src/lib/brand.js``), mapped from
    the :class:`LoginBrand` server config. ``logo`` may be inline SVG or an emoji.
    """
    return {
        "productName": brand.product_name,
        "tagline": brand.tagline,
        "logoSvg": brand.logo or None,
        "accent": brand.safe_accent(),
        "footer": brand.footer,
    }


def _inject_brand(index_html: str, brand: LoginBrand) -> str:
    """Splice ``window.__AXIOM_GATE_BRAND__`` into the built ``index.html`` head,
    before the module script so the global is set before the bundle boots.

    The brand is trusted config, but we JSON-encode it and neutralize every ``<``
    (``\\u003c``) anyway so a stray ``</script>`` in a logo SVG can't break out of
    the inline script.
    """
    brand_json = json.dumps(_spa_brand_payload(brand)).replace("<", "\\u003c")
    snippet = f"<script>window.__AXIOM_GATE_BRAND__ = {brand_json};</script>"
    marker = '<script type="module"'
    idx = index_html.find(marker)
    if idx != -1:
        return index_html[:idx] + snippet + index_html[idx:]
    head_close = index_html.lower().find("</head>")
    if head_close != -1:
        return index_html[:head_close] + snippet + index_html[head_close:]
    return snippet + index_html


def build_webgate_router(
    user_store: UserStore | None = None,
    *,
    secure_cookies: bool | None = None,
    brand: LoginBrand | None = None,
    spa_dist: Path | None = None,
) -> APIRouter:
    """Assemble the ``/gate`` forward-auth router.

    ``user_store`` defaults to the process-wide store; ``secure_cookies`` defaults
    to True (set the ``Secure`` flag — requires HTTPS) unless overridden for a
    local http bring-up; ``brand`` skins the login UI (brand-neutral when omitted).

    ``spa_dist`` is the optional path to a built Vite bundle (``dist/``). When it
    is given AND ``spa_dist/index.html`` exists, the ``GET /gate/login`` and
    ``GET /gate/forgot`` routes serve that single brand-neutral bundle with the
    brand injected at ``window.__AXIOM_GATE_BRAND__``, and static assets are
    served from ``spa_dist/assets``. When it is absent (tests, or any deploy
    without a build) everything behaves exactly as the server-rendered default.
    """
    router = APIRouter(tags=["webgate"])
    secure = _default_secure() if secure_cookies is None else secure_cookies
    the_brand = brand if brand is not None else LoginBrand()

    spa_root = spa_dist if spa_dist is None else Path(spa_dist)
    spa_active = spa_root is not None and (spa_root / "index.html").is_file()
    branded_index = (
        _inject_brand((spa_root / "index.html").read_text(encoding="utf-8"), the_brand)
        if spa_active
        else ""
    )
    assets_root = (spa_root / "assets").resolve() if spa_active else None

    def _store() -> UserStore:
        return user_store if user_store is not None else get_user_store()

    def _set_session(response: Response, token: str, *, ttl: timedelta) -> None:
        response.set_cookie(
            SESSION_COOKIE, token, max_age=int(ttl.total_seconds()),
            httponly=True, secure=secure, samesite="lax", path="/",
        )

    if spa_active:
        # Serve the built Vite bundle. The SPA reads `?next=` client-side and owns
        # its own /login and /forgot routes (react-router basename="/gate"), so
        # both paths serve the same brand-injected index.html.
        @router.get("/gate/login")
        async def login_page() -> HTMLResponse:
            return HTMLResponse(branded_index)

        @router.get("/gate/forgot")
        async def forgot_page() -> HTMLResponse:
            return HTMLResponse(branded_index)

        @router.get("/gate/assets/{path:path}")
        async def assets(path: str) -> Response:
            # Resolve under assets_root and confirm it stays inside — a `..` in the
            # request resolves out of the tree and is refused.
            target = (assets_root / path).resolve()
            try:
                target.relative_to(assets_root)
            except ValueError:
                return Response(status_code=404)
            if not target.is_file():
                return Response(status_code=404)
            return FileResponse(target)

        _favicon = spa_root / "favicon.svg"
        if _favicon.is_file():
            @router.get("/gate/favicon.svg")
            async def favicon() -> Response:
                return FileResponse(_favicon, media_type="image/svg+xml")
    else:
        @router.get("/gate/login")
        async def login_page(request: Request) -> HTMLResponse:
            # Accept `return_to` as an alias for `next` so the oauth AS's login
            # redirect (which uses return_to) round-trips back through authorize.
            q = request.query_params
            return _login_page(q.get("next") or q.get("return_to") or "/", the_brand)

        @router.get("/gate/forgot")
        async def forgot_page() -> HTMLResponse:
            return _forgot_page(the_brand)

    @router.post("/gate/login")
    async def login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
        remember: str = Form(""),
    ) -> Response:
        user = authenticate(_store(), email, password)
        if user is None:
            return _login_page(next, the_brand, error="Invalid email or password.")
        ttl = REMEMBER_TTL if remember else DEFAULT_SESSION_TTL
        token = issue_session_token(user, ttl=ttl, issuer=_issuer(request))
        redirect = RedirectResponse(_safe_next(next), status_code=303)
        _set_session(redirect, token, ttl=ttl)
        return redirect

    @router.post("/gate/session")
    async def session_login(request: Request, body: _SessionLogin) -> Response:
        # The XHR/JSON login seam for the Vite UI: inline errors (no full-page
        # redirect), same cookie session as the form path. A SoilMetrix-style
        # client converges onto this by pointing at it with credentials:'include'.
        user = authenticate(_store(), body.email, body.password)
        if user is None:
            return JSONResponse({"detail": "Incorrect email or password"}, status_code=401)
        ttl = REMEMBER_TTL if body.remember else DEFAULT_SESSION_TTL
        token = issue_session_token(user, ttl=ttl, issuer=_issuer(request))
        resp = JSONResponse(_user_public(user))
        _set_session(resp, token, ttl=ttl)
        return resp

    @router.get("/gate/me")
    async def me(request: Request) -> Response:
        # The "am I logged in?" probe the SPA calls on boot (replaces a bearer
        # /auth/me). 200 + identity JSON on a valid session, else 401 + {detail}.
        claims = session_from_cookies(request.cookies, issuer=_issuer(request))
        if claims is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return JSONResponse(_user_public(claims))

    @router.get("/gate/verify")
    async def verify(request: Request) -> Response:
        claims = session_from_cookies(request.cookies, issuer=_issuer(request))
        if claims is None:
            return Response(status_code=401)
        return Response(
            status_code=200,
            headers={
                "X-Axiom-User-Id": str(claims.get("sub", "")),
                "X-Axiom-User-Email": str(claims.get("email", "")),
                "X-Axiom-User-Name": str(claims.get("name", "")),
                "X-Axiom-User-Roles": ",".join(claims.get("roles", []) or []),
            },
        )

    @router.api_route("/gate/logout", methods=["GET", "POST"])
    async def logout() -> Response:
        # GET so a browser *navigation* logs out too, not just the SPA's fetch
        # (POST): a consumer UI's post-signout redirect can point here to end the
        # gate session in one hop — e.g. Open WebUI's WEBUI_AUTH_SIGNOUT_REDIRECT_URL,
        # which exists precisely so a trusted-header UI isn't re-authed straight
        # back in. Either method clears the session cookie and returns to login.
        redirect = RedirectResponse("/gate/login", status_code=303)
        redirect.delete_cookie(SESSION_COOKIE, path="/")
        return redirect

    return router


__all__ = ["LoginBrand", "build_webgate_router"]
