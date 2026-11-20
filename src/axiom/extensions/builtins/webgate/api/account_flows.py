# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Account lifecycle flows — the SoilMetrix login feature set, out of the box.

Ported behaviors (founder direction 2026-09-24: everything the Field
Hand's login supports, supported here), each carried with the property
that made it hard-won:

- **forgot / reset password** — enumeration-proof (same 200 whatever the
  email), signed single-use token (1h) bound to the account's token
  epoch, strength-validated, changed-password notice mailed after.
- **magic link** — passwordless sign-in for EXISTING accounts only (an
  unauthenticated POST must never become an account-creation + email
  amplifier); single-use, short-lived, hashed at rest via the platform
  magic-link core; redeeming verifies the email address.
- **email verification** — signed verify token; resend is
  enumeration-proof; clicking marks ``email_verified_at``.
- **self-signup** — POLICY-GATED (``AXIOM_GATE_SELF_SIGNUP``), off by
  default: a site chooses to be sign-up-able; response never reveals
  whether the email already existed.
- **change password** (epoch bump), **password strength
  probe** (for signup forms), **profile update**, **provider listing**
  (the SPA's sign-in buttons), and the **dev email log** (development/
  test environments only — the outbox holds live tokens).

Token epochs: ``attributes["token_version"]`` — reset/magic credentials
are minted against the current epoch and every successful use bumps it,
so a link can be used exactly once and older links die with it.
"""

# NOTE: no `from __future__ import annotations` (FastAPI resolves handler
# annotations against module globals — same rule as fleet/api.py).

import os
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from axiom.infra.state import LockedJsonFile
from axiom.webauth import validate_password
from axiom.webauth.jwt import create_access_token, verify_token
from axiom.webauth.magic_link import (
    MagicLinkError,
    MagicLinkRecord,
    issue_magic_link,
    redeem_magic_link,
)
from axiom.webauth.password import get_password_hash, verify_password
from axiom.webauth.session import session_from_cookies
from axiom.webauth.users import User, WritableUserStore

from ..email_outbox import OutboundEmail, dev_email_log_enabled, get_sender

RESET_TTL = timedelta(hours=1)
VERIFY_TTL = timedelta(hours=24)
MAGIC_LINKS_ENV = "AXIOM_GATE_MAGIC_LINKS_FILE"
SELF_SIGNUP_ENV = "AXIOM_GATE_SELF_SIGNUP"

#: Enumeration-proof generics (one string per flow, never varied).
GENERIC_RESET = "If that email is registered, a reset link has been sent."
GENERIC_MAGIC = "If that email has an account, a sign-in link is on its way."
GENERIC_VERIFY = "If that email is registered, a verification link has been sent."
GENERIC_SIGNUP = "Check your email to finish setting up your account."


def self_signup_enabled() -> bool:
    return os.environ.get(SELF_SIGNUP_ENV, "").lower() in {"1", "true", "yes"}


def _epoch(user: User) -> int:
    attrs = user.attributes if isinstance(user.attributes, dict) else {}
    try:
        return int(attrs.get("token_version", 0))
    except (TypeError, ValueError):
        return 0


def _with(user: User, **attr_updates: Any) -> User:
    attrs = dict(user.attributes or {})
    attrs.update(attr_updates)
    return User(
        user_id=user.user_id,
        email=user.email,
        password_hash=user.password_hash,
        name=user.name,
        roles=user.roles,
        disabled=user.disabled,
        attributes=attrs,
        site=user.site,
    )


def _bump(user: User) -> User:
    return _with(user, token_version=_epoch(user) + 1)


class JsonFileMagicLinkStore:
    """The magic-link core's durable home as a locked JSON file — the same
    at-rest posture as the gate's users/API-keys files (dev + single-node;
    a Postgres store is the multi-node follow-up)."""

    def __init__(self, path: "str | os.PathLike | None" = None) -> None:
        raw = str(path) if path else os.environ.get(MAGIC_LINKS_ENV, "")
        if not raw:
            raise ValueError(f"JsonFileMagicLinkStore needs a path (or ${MAGIC_LINKS_ENV})")
        self._path = Path(raw)

    def _all(self) -> dict:
        if not self._path.is_file():
            return {}
        import json

        try:
            return json.loads(self._path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}  # fail closed: unreadable = no redeemable links

    def put(self, record: MagicLinkRecord) -> None:
        with LockedJsonFile(self._path, exclusive=True) as f:
            data = f.read() or {}
            data[record.link_id] = record.__dict__.copy()
            f.write(data)

    def get(self, link_id: str) -> MagicLinkRecord | None:
        raw = self._all().get(link_id)
        return MagicLinkRecord(**raw) if raw else None

    def mark_used(self, link_id: str, used_at: float) -> None:
        with LockedJsonFile(self._path, exclusive=True) as f:
            data = f.read() or {}
            if link_id in data:
                data[link_id]["used_at"] = used_at
            f.write(data)


# ------------------------------------------------------------------ bodies
class _Email(BaseModel):
    email: str


class _Reset(BaseModel):
    token: str
    new_password: str


class _ChangePassword(BaseModel):
    current_password: str
    new_password: str


class _ValidatePassword(BaseModel):
    password: str


class _Register(BaseModel):
    email: str
    password: str
    name: str = ""


class _Profile(BaseModel):
    name: str


def register_account_routes(
    router: APIRouter,
    *,
    store: Callable[[], Any],
    issuer_of: Callable[[Request], str],
    set_session: Callable[..., None],
    shell: Callable[..., HTMLResponse],
    providers: Callable[[], list],
    default_site: Callable[[], "str | None"],
    magic_store: Callable[[], Any] | None = None,
) -> None:
    """Attach the account flows to the gate router. Every dependency is a
    closure from ``build_webgate_router`` so the flows share the gate's
    store, issuer, cookie, and brand exactly."""

    the_magic_store = magic_store or (lambda: JsonFileMagicLinkStore())

    def _claims(request: Request) -> dict | None:
        return session_from_cookies(request.cookies, issuer=issuer_of(request))

    def _writable(s: Any) -> bool:
        return isinstance(s, WritableUserStore)

    def _mint(user: User, request: Request, *, type_: str, ttl: timedelta) -> str:
        return create_access_token(
            {
                "sub": user.user_id,
                "email": user.email,
                "type": type_,
                "tv": _epoch(user),
            },
            expires_delta=ttl,
            issuer=issuer_of(request),
        )

    def _typed(token: str, request: Request, *, type_: str) -> dict | None:
        claims = verify_token(token, issuer=issuer_of(request))
        if claims is None or claims.get("type") != type_:
            return None
        return claims

    def _mail(kind: str, to: str, subject: str, body: str) -> None:
        get_sender().send(OutboundEmail(to=to, subject=subject, body=body, kind=kind))

    def _base(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    # (logout already ships in the gate core: GET|POST /gate/logout → 303.)
    # -------------------------------------------------- password: probe/change
    @router.post("/gate/password/validate")
    async def password_validate(body: _ValidatePassword) -> dict:
        ok, message = validate_password(body.password, complexity="standard")
        return {"ok": bool(ok), "message": "" if ok else str(message)}

    @router.put("/gate/password")
    async def password_change(request: Request, body: _ChangePassword) -> Response:
        claims = _claims(request)
        if claims is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        s = store()
        user = s.get_by_id(str(claims.get("sub", ""))) or s.get_by_email(
            str(claims.get("email", ""))
        )
        if user is None or not user.password_hash:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if not verify_password(body.current_password, user.password_hash):
            return JSONResponse({"detail": "Current password is incorrect"}, status_code=400)
        ok, message = validate_password(body.new_password, complexity="standard")
        if not ok:
            return JSONResponse({"detail": str(message)}, status_code=400)
        if not _writable(s):
            return JSONResponse({"detail": "account store is read-only"}, status_code=501)
        updated = _bump(
            User(
                user_id=user.user_id,
                email=user.email,
                password_hash=get_password_hash(body.new_password),
                name=user.name,
                roles=user.roles,
                disabled=user.disabled,
                attributes=dict(user.attributes or {}),
                site=user.site,
            )
        )
        s.upsert(updated)
        _mail(
            "notice",
            user.email,
            "Your password was changed",
            "Your account password was just changed. If this wasn't you, use "
            "the password-reset link on the sign-in page immediately.",
        )
        return JSONResponse({"detail": "password changed"})

    # ------------------------------------------------------- forgot / reset
    @router.post("/gate/forgot")
    async def forgot(request: Request, body: _Email) -> dict:
        # Always the same answer — never reveal whether the email exists.
        user = store().get_by_email(body.email)
        if user is not None and not user.disabled:
            token = _mint(user, request, type_="reset", ttl=RESET_TTL)
            _mail(
                "reset",
                user.email,
                "Reset your password",
                f"Reset your password (valid 1 hour, single use):\n"
                f"{_base(request)}/gate/reset?token={token}",
            )
        return {"detail": GENERIC_RESET}

    @router.get("/gate/reset")
    async def reset_page(token: str = "") -> HTMLResponse:
        inner = (
            "<h1>Choose a new password</h1>"
            '<form id="f"><input type="hidden" id="tok" value="' + token.replace('"', "") + '">'
            '<label for="pw">New password</label>'
            '<input id="pw" type="password" autocomplete="new-password" required>'
            '<button type="submit">Set password</button></form><p id="msg"></p>'
            "<script>document.getElementById('f').onsubmit=async(e)=>{e.preventDefault();"
            "const r=await fetch('/gate/reset',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({token:document.getElementById('tok').value,new_password:document.getElementById('pw').value})});"
            "const j=await r.json();document.getElementById('msg').textContent=j.detail||'';"
            "if(r.ok){setTimeout(()=>{location='/gate/login'},1200)}};</script>"
        )
        return shell(title="Reset password", inner=inner)

    @router.post("/gate/reset")
    async def reset(request: Request, body: _Reset) -> Response:
        claims = _typed(body.token, request, type_="reset")
        if claims is None:
            return JSONResponse({"detail": "Invalid or expired reset link"}, status_code=401)
        s = store()
        user = s.get_by_id(str(claims.get("sub", "")))
        if user is None:
            return JSONResponse({"detail": "Invalid or expired reset link"}, status_code=401)
        # Single-use: the token carries the epoch it was minted against.
        if int(claims.get("tv", -1)) != _epoch(user):
            return JSONResponse(
                {"detail": "This reset link has already been used or is no longer valid"},
                status_code=401,
            )
        ok, message = validate_password(body.new_password, complexity="standard")
        if not ok:
            return JSONResponse({"detail": str(message)}, status_code=400)
        if not _writable(s):
            return JSONResponse({"detail": "account store is read-only"}, status_code=501)
        updated = _bump(
            User(
                user_id=user.user_id,
                email=user.email,
                password_hash=get_password_hash(body.new_password),
                name=user.name,
                roles=user.roles,
                disabled=user.disabled,
                attributes=dict(user.attributes or {}),
                site=user.site,
            )
        )
        s.upsert(updated)
        _mail(
            "notice",
            user.email,
            "Your password was changed",
            "Your password was just reset. If this wasn't you, reset it again "
            "immediately and contact your administrator.",
        )
        return JSONResponse({"detail": "Password reset successfully"})

    # ------------------------------------------------------ email verification
    def _send_verify(user: User, request: Request) -> None:
        token = _mint(user, request, type_="verify_email", ttl=VERIFY_TTL)
        _mail(
            "verify",
            user.email,
            "Verify your email",
            f"Confirm this address belongs to you (valid 24 hours):\n"
            f"{_base(request)}/gate/verify-email?token={token}",
        )

    @router.post("/gate/verify-email/resend")
    async def resend_verification(request: Request, body: _Email) -> dict:
        user = store().get_by_email(body.email)
        if user is not None and not user.disabled:
            attrs = user.attributes or {}
            if not attrs.get("email_verified_at"):
                _send_verify(user, request)
        return {"detail": GENERIC_VERIFY}

    @router.get("/gate/verify-email")
    async def verify_email(request: Request, token: str = "") -> HTMLResponse:
        claims = _typed(token, request, type_="verify_email")
        s = store()
        user = s.get_by_id(str(claims.get("sub", ""))) if claims else None
        if user is None:
            return shell(
                title="Verification",
                inner="<h1>That link is invalid or has expired</h1>"
                '<p><a href="/gate/login">Back to sign in</a></p>',
                status=401,
            )
        if _writable(s) and not (user.attributes or {}).get("email_verified_at"):
            s.upsert(_with(user, email_verified_at=time.time()))
        return shell(
            title="Email verified",
            inner="<h1>Email verified</h1>"
            '<p>You can close this tab, or <a href="/gate/login">sign in</a>.</p>',
        )

    # ------------------------------------------------------------- magic link
    @router.post("/gate/magic-link")
    async def magic_link(request: Request, body: _Email) -> dict:
        # EXISTING active accounts only — never an account-creation amplifier.
        user = store().get_by_email(body.email)
        if user is not None and not user.disabled:
            site = user.site or default_site() or "local"
            token = issue_magic_link(user.email, site, roles=user.roles, store=the_magic_store())
            _mail(
                "magic_link",
                user.email,
                "Your sign-in link",
                f"Sign in with one click (valid 15 minutes, single use):\n"
                f"{_base(request)}/gate/magic?token={token}",
            )
        return {"detail": GENERIC_MAGIC}

    @router.get("/gate/magic")
    async def magic_redeem(request: Request, token: str = "", next: str = "/") -> Response:
        try:
            redeemed = redeem_magic_link(token, store=the_magic_store())
        except MagicLinkError:
            return shell(
                title="Sign-in link",
                inner="<h1>This link is invalid or has expired</h1>"
                '<p><a href="/gate/login">Back to sign in</a></p>',
                status=401,
            )
        s = store()
        user = s.get_by_email(redeemed.email)
        if user is None or user.disabled:
            return shell(
                title="Sign-in link",
                inner="<h1>This link is invalid or has expired</h1>"
                '<p><a href="/gate/login">Back to sign in</a></p>',
                status=401,
            )
        # Clicking a link mailed to the address proves the address.
        if _writable(s) and not (user.attributes or {}).get("email_verified_at"):
            user = _with(user, email_verified_at=time.time())
            s.upsert(user)
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        resp = RedirectResponse(target, status_code=303)
        set_session(resp, user, request)
        return resp

    # ------------------------------------------------------------ self-signup
    @router.get("/gate/signup")
    async def signup_page() -> HTMLResponse:
        if not self_signup_enabled():
            return shell(
                title="Sign up",
                inner="<h1>Sign-up is by invitation here</h1>"
                "<p>Ask your administrator for an invite, or "
                '<a href="/gate/login">sign in</a>.</p>',
                status=404,
            )
        inner = (
            "<h1>Create your account</h1>"
            '<form id="su">'
            '<label for="se">Email</label>'
            '<input id="se" type="email" autocomplete="email" required>'
            '<label for="sn">Name</label>'
            '<input id="sn" type="text" autocomplete="name">'
            '<label for="sp">Password</label>'
            '<input id="sp" type="password" autocomplete="new-password" required>'
            '<button type="submit">Create account</button></form>'
            '<p id="sm" aria-live="polite"></p>'
            '<p class="alt"><a href="/gate/login">← Back to sign in</a></p>'
            "<script>document.getElementById('su').onsubmit=async(e)=>{e.preventDefault();"
            "const r=await fetch('/gate/register',{method:'POST',"
            "headers:{'Content-Type':'application/json'},body:JSON.stringify({"
            "email:document.getElementById('se').value,"
            "name:document.getElementById('sn').value,"
            "password:document.getElementById('sp').value})});"
            "const j=await r.json();document.getElementById('sm').textContent=j.detail||'';};"
            "</script>"
        )
        return shell(title="Create account", inner=inner)

    @router.post("/gate/register")
    async def register(request: Request, body: _Register) -> Response:
        if not self_signup_enabled():
            return JSONResponse({"detail": "Not found"}, status_code=404)
        ok, message = validate_password(body.password, complexity="standard")
        if not ok:
            return JSONResponse({"detail": str(message)}, status_code=400)
        s = store()
        if not _writable(s):
            return JSONResponse({"detail": "account store is read-only"}, status_code=501)
        existing = s.get_by_email(body.email)
        if existing is None:
            user = User(
                user_id=body.email,
                email=body.email,
                password_hash=get_password_hash(body.password),
                name=body.name,
                roles=(),
                site=default_site(),
            )
            s.upsert(user)
            _send_verify(user, request)
        # The same answer either way — signup must not confirm existence.
        return JSONResponse({"detail": GENERIC_SIGNUP})

    # ---------------------------------------------------------------- profile
    @router.put("/gate/profile")
    async def profile_update(request: Request, body: _Profile) -> Response:
        claims = _claims(request)
        if claims is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        s = store()
        user = s.get_by_id(str(claims.get("sub", "")))
        if user is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        if not _writable(s):
            return JSONResponse({"detail": "account store is read-only"}, status_code=501)
        updated = User(
            user_id=user.user_id,
            email=user.email,
            password_hash=user.password_hash,
            name=body.name.strip(),
            roles=user.roles,
            disabled=user.disabled,
            attributes=dict(user.attributes or {}),
            site=user.site,
        )
        s.upsert(updated)
        return JSONResponse({"detail": "profile updated", "name": updated.name})

    # ------------------------------------------------- providers + dev outbox
    @router.get("/gate/oidc/providers")
    async def oidc_providers() -> dict:
        return {
            "providers": [
                {"name": p.name, "label": getattr(p, "label", p.name), "login_path": p.login_path}
                for p in providers()
            ],
            "self_signup": self_signup_enabled(),
        }

    @router.get("/gate/dev/email-log")
    async def dev_email_log() -> Response:
        # Live tokens inside — development/test environments only (ported rule).
        if not dev_email_log_enabled():
            return JSONResponse({"detail": "Not found"}, status_code=404)
        sender = get_sender()
        sent = sender.sent() if hasattr(sender, "sent") else []
        return JSONResponse({"emails": sent})


__all__ = [
    "GENERIC_MAGIC",
    "GENERIC_RESET",
    "GENERIC_SIGNUP",
    "GENERIC_VERIFY",
    "JsonFileMagicLinkStore",
    "register_account_routes",
    "self_signup_enabled",
]
