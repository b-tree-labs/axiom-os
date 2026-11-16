# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OIDC sign-in for the gate — "Sign in with <your institution>" (ADR-075 × ADR-003).

The password form is the gate's floor; this is the fast-follow the docstrings
have promised. It rides the ``auth`` extension's building blocks unchanged —
:func:`authorization_url` / :func:`exchange_code` (PKCE S256, state, nonce) and
:func:`verify_id_token` (JWKS signature + iss/aud/exp/nbf) — and ends in the
**same** ``webauth`` session cookie the password path mints, so ``/gate/verify``,
the forward-auth headers, and the OAuth bridge need no changes.

Flow::

    GET /gate/oidc/login?next=/chat
        → mint a signed transaction (state, nonce, PKCE verifier, next, remember)
          into a short-lived HttpOnly cookie, 302 to the IdP
    GET /gate/oidc/callback?code=…&state=…
        → verify the transaction, exchange the code, verify the id_token against
          the IdP's JWKS, check the nonce, resolve/create the account, issue the
          gate session, clear the transaction cookie, 303 to ``next``

Accounts are keyed on the IdP's **immutable subject** (Entra ``oid`` by default,
``sub`` for everyone else) so a name or email change never orphans a person.
An existing password account with the same email is *linked* rather than
duplicated. Roles come from the IdP's ``roles`` claim when it is present (an
Entra app-role assignment driven by group membership); when the claim is
absent, locally-assigned roles are kept.

Configuration is environment-driven so the same ``build_webgate_router()`` call
turns it on with no code change at the consumer::

    AXIOM_GATE_OIDC_PROVIDER=entra              # entra | google | <issuer URL>
    AXIOM_GATE_OIDC_TENANT=<tenant-guid>        # entra only
    AXIOM_GATE_OIDC_CLIENT_ID=<app (client) id>
    AXIOM_GATE_OIDC_CLIENT_SECRET_FILE=~/.config/axiom/gate-oidc.secret   # mode 0600
    AXIOM_GATE_OIDC_LABEL="Sign in with your institution"
    AXIOM_GATE_OIDC_JIT=1                       # create accounts on first sign-in

A **public client** (``AXIOM_GATE_OIDC_CLIENT_TYPE=public``) is the local
development shape: authorization code + PKCE with no client secret at all, and
the loopback redirect ``http://localhost/gate/oidc/callback`` (or
``127.0.0.1``) that the identity provider registered for a public-client
platform. No secret setting is read for it, and none is sent.

Nothing here holds the client secret in a module global, logs a token, or trusts
an unverified claim.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from axiom.extensions.builtins.auth.flow import authorization_url, exchange_code
from axiom.extensions.builtins.auth.jwt_verify import TokenVerificationError, verify_id_token
from axiom.extensions.builtins.auth.pkce import generate_pkce
from axiom.extensions.builtins.auth.providers import IdpConfig, entra, from_discovery, google
from axiom.webauth import User, UserStore, create_access_token, verify_token
from axiom.webauth.users import WritableUserStore

_LOG = logging.getLogger("axiom.webgate.oidc")

#: The transaction cookie carrying state / nonce / PKCE verifier between the two
#: legs of the flow. Short-lived, HttpOnly, SameSite=Lax (the callback is a
#: top-level navigation from the IdP, which Lax permits).
TXN_COOKIE = "axiom_oidc_txn"
DEFAULT_TXN_TTL = timedelta(minutes=10)
_TXN_TYPE = "oidc_txn"

DEFAULT_SCOPES: tuple[str, ...] = ("openid", "profile", "email")
DEFAULT_LABEL = "Sign in with single sign-on"

#: Hosts a public client's redirect may use: the two loopback forms a
#: public-client platform registers (``http://localhost/...`` and
#: ``http://127.0.0.1/...``).
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
#: The redirects a site registers for local development, port-less.
LOOPBACK_REDIRECTS = (
    "http://localhost/gate/oidc/callback",
    "http://127.0.0.1/gate/oidc/callback",
)
CLIENT_TYPES = ("confidential", "public")
_FALSEY = {"0", "false", "no", "off", ""}


class OidcSignInError(Exception):
    """A sign-in leg failed. ``public`` is the sentence safe to show the user."""

    def __init__(self, public: str, detail: str = "") -> None:
        super().__init__(detail or public)
        self.public = public
        self.detail = detail


# ---------------------------------------------------------------- transport


class IdpHttpError(Exception):
    """The identity provider answered an error. ``error`` / ``description`` are
    the OAuth error fields (RFC 6749 section 5.2) when it sent them."""

    def __init__(self, status: int, error: str = "", description: str = "") -> None:
        super().__init__(f"HTTP {status} {error}".strip())
        self.status = status
        self.error = error
        self.description = description


class UrllibHttp:
    """The minimal ``post(url, data) -> dict`` / ``get(url) -> dict`` client the
    ``auth`` flow functions expect, on the standard library. Injected in tests."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def post(self, url: str, data: Mapping[str, str]) -> dict:
        body = urlencode(dict(data)).encode()
        req = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — IdP endpoint from config
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            # Keep the OAuth error fields: "redirect not registered" and "this
            # app wants a secret" each have a fix to name, and both arrive here.
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001 — an unreadable body is just an HTTP error
                body = {}
            body = body if isinstance(body, dict) else {}
            raise IdpHttpError(
                exc.code, str(body.get("error") or ""), str(body.get("error_description") or "")
            ) from None

    def get(self, url: str) -> dict:
        req = Request(url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 — IdP endpoint from config
            return json.loads(resp.read().decode("utf-8"))


class JwksCache:
    """The IdP's signing keys, fetched lazily and refreshed once on a ``kid`` miss
    (key rotation) — never on every request."""

    def __init__(self, http: Any, jwks_uri: str) -> None:
        self._http = http
        self._uri = jwks_uri
        self._jwks: dict | None = None
        self._lock = threading.Lock()

    def _fetch(self) -> dict:
        doc = self._http.get(self._uri)
        if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
            raise OidcSignInError(
                "The sign-in provider's keys could not be read.", f"malformed JWKS from {self._uri}"
            )
        return doc

    def get(self, kid: str | None) -> dict:
        with self._lock:
            if self._jwks is None or (
                kid and not any(k.get("kid") == kid for k in self._jwks.get("keys", []))
            ):
                self._jwks = self._fetch()
            return self._jwks


def _kid_of(id_token: str) -> str | None:
    try:
        import base64

        header = id_token.split(".")[0]
        return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))).get("kid")
    except Exception:  # noqa: BLE001 — a malformed header fails verification next anyway
        return None


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class OidcSignIn:
    """One configured upstream IdP for the gate's SSO button."""

    idp: IdpConfig
    client_id: str
    client_secret: str | None = None
    redirect_path: str = "/gate/oidc/callback"
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    label: str = DEFAULT_LABEL
    #: Claim that identifies the person immutably. Entra's ``oid`` (object id)
    #: survives renames; ``sub`` is the fallback for any IdP.
    subject_claim: str = "oid"
    roles_claim: str = "roles"
    #: Create an account on first successful sign-in. Off = only pre-provisioned
    #: accounts (matched by subject or email) may enter.
    jit: bool = True
    txn_ttl: timedelta = DEFAULT_TXN_TTL
    extra_authorize_params: dict = field(default_factory=dict)
    name: str = "sso"
    kind: str = "oidc"
    #: A public client: PKCE alone proves the caller, no secret exists, and the
    #: redirect is the loopback form a public-client platform registers.
    public: bool = False
    #: What a just-in-time account is keyed on: the IdP's ``subject`` (a
    #: deployed node) or the person's ``email`` (the shape ``dev up`` makes).
    account_id: str = "subject"
    #: Take roles from the IdP's roles claim. Off on a dev node, where the
    #: person running it owns it whatever the site's app roles say.
    idp_roles: bool = True
    #: The issuer's discovery document could not be read when the gate was
    #: built. The gate still stands and this provider refuses to start a
    #: sign-in until :func:`rediscover` reads the document (#1842): a provider
    #: that cannot be reached must never take the gate down with it.
    unreachable: bool = False

    def redirect_uri(self, base_url: str) -> str:
        """Where the IdP sends the browser back, as the IdP will compare it.

        A confidential client: this node's own address. A public client: the
        loopback form (``http://localhost`` or ``http://127.0.0.1``) with this
        node's port, which the browser needs to reach the node; identity
        providers ignore the port when matching a loopback redirect registered
        on a public-client platform (RFC 8252 section 7.3)."""
        if not self.public:
            return base_url.rstrip("/") + self.redirect_path
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower()
        if host not in LOOPBACK_HOSTS:
            raise OidcSignInError(
                "Single sign-on for local development works only at a loopback address. "
                f"Open this node at http://localhost:{parts.port or 80}/ and sign in again.",
                f"public client asked to redirect to non-loopback host {host!r}",
            )
        port = f":{parts.port}" if parts.port else ""
        return f"http://{host}{port}{self.redirect_path}"

    def registered_redirect(self, base_url: str = "") -> str:
        """The redirect as the site's app registration must list it (no port)."""
        host = (urlsplit(base_url).hostname or "").lower() if base_url else ""
        return f"http://{host if host in LOOPBACK_HOSTS else 'localhost'}{self.redirect_path}"

    @property
    def login_path(self) -> str:
        return "/gate/oidc/login" if self.name == "sso" else f"/gate/oidc/{self.name}/login"

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, http: Any = None
    ) -> OidcSignIn | None:
        """Build the primary provider from ``AXIOM_GATE_OIDC_*``; ``None`` when
        SSO is not configured. A half-configured setup (provider without client
        id, entra without tenant) is refused loudly rather than silently
        degrading to password-only."""
        return cls._one_from_env(
            os.environ if env is None else env, prefix="AXIOM_GATE_OIDC_", name="sso", http=http
        )

    @classmethod
    def _one_from_env(
        cls, env: Mapping[str, str], *, prefix: str, name: str, http: Any = None
    ) -> OidcSignIn | None:
        def get(key: str) -> str:
            return (env.get(prefix + key) or "").strip()

        provider = get("PROVIDER").lower()
        client_id = get("CLIENT_ID")
        if not provider and not client_id:
            return None
        if not provider or not client_id:
            raise ValueError(
                f"half-configured OIDC provider {name!r}: both {prefix}PROVIDER "
                f"and {prefix}CLIENT_ID are required"
            )
        preset = None
        unreachable = False
        if provider == "entra":
            tenant = get("TENANT")
            if not tenant:
                raise ValueError(f"{prefix}PROVIDER=entra requires {prefix}TENANT")
            idp = entra(tenant)
            subject_default = "oid"
            kind = "entra"
        elif provider == "google":
            idp = google()
            subject_default = "sub"
            kind = "google"
        elif provider.startswith("https://"):
            try:
                idp = from_discovery(http or UrllibHttp(), provider)
            except Exception as exc:  # noqa: BLE001 — any failure to read the document
                # Fail closed (#1842). Raising here dropped the whole gate
                # mount, and a node served without its gate is open.
                _LOG.warning(
                    "sign-in provider %r cannot be reached (%s); the gate stays up and "
                    "this provider refuses sign-in until it answers",
                    name,
                    type(exc).__name__,
                )
                idp = _unreached(provider)
                unreachable = True
            preset = issuer_preset(provider)
            subject_default = preset.subject if preset else "sub"
            kind = preset.kind if preset else "oidc"
        else:
            raise ValueError(f"unknown {prefix}PROVIDER {provider!r}")
        client_type = (get("CLIENT_TYPE") or "confidential").lower()
        if client_type not in CLIENT_TYPES:
            raise ValueError(f"{prefix}CLIENT_TYPE must be one of {', '.join(CLIENT_TYPES)}")
        public = client_type == "public"
        secret: str | None = None
        if public:
            # A public client has no secret. One configured alongside it is a
            # contradiction, refused loudly; none is ever read for it.
            named = [k for k in ("CLIENT_SECRET", "CLIENT_SECRET_FILE", "CLIENT_SECRET_VAULT") if get(k)]
            if named:
                raise ValueError(
                    f"{prefix}CLIENT_TYPE=public takes no secret, but {prefix}{named[0]} is set"
                )
        else:
            secret = get("CLIENT_SECRET") or None
            secret_file = get("CLIENT_SECRET_FILE")
            if secret is None and secret_file:
                secret = (
                    Path(os.path.expanduser(secret_file)).read_text(encoding="utf-8").strip()
                    or None
                )
            secret_vault = get("CLIENT_SECRET_VAULT")
            if secret is None and secret_vault:
                secret = _secret_from_vault(secret_vault, f"{prefix}CLIENT_SECRET_VAULT")
        account_id = (get("ACCOUNT_ID") or "subject").lower()
        if account_id not in ("subject", "email"):
            raise ValueError(f"{prefix}ACCOUNT_ID must be 'subject' or 'email'")
        scopes_raw = get("SCOPES").split()
        default_redirect = "/gate/oidc/callback" if name == "sso" else f"/gate/oidc/{name}/callback"
        if name == "sso":
            default_label = DEFAULT_LABEL
        elif preset is not None:
            default_label = f"Continue with {preset.brand}"
        else:
            default_label = f"Continue with {name.replace('_', ' ').title()}"
        return cls(
            idp=idp,
            client_id=client_id,
            client_secret=secret,
            redirect_path=(get("REDIRECT_PATH") or default_redirect),
            scopes=tuple(scopes_raw) if scopes_raw else DEFAULT_SCOPES,
            label=(get("LABEL") or default_label),
            subject_claim=(get("SUBJECT_CLAIM") or subject_default),
            roles_claim=(get("ROLES_CLAIM") or "roles"),
            jit=(env.get(prefix + "JIT", "1").strip().lower() not in _FALSEY),
            name=name,
            kind=kind,
            public=public,
            account_id=account_id,
            idp_roles=(env.get(prefix + "IDP_ROLES", "1").strip().lower() not in _FALSEY),
            unreachable=unreachable,
        )


#: What a person reads when the provider cannot be reached.
UNREACHABLE = "The sign-in provider cannot be reached right now. Try again shortly."


def _unreached(issuer: str) -> IdpConfig:
    """A provider known only by its issuer: no endpoint to send anybody to."""
    return IdpConfig(name=issuer, authorization_endpoint="", token_endpoint="", issuer=issuer)


def rediscover(cfg: OidcSignIn, http: Any = None) -> OidcSignIn:
    """``cfg`` with its issuer's endpoints, read now; ``cfg`` itself when it has them.

    Raises :class:`OidcSignInError` while the issuer still does not answer."""
    if not cfg.unreachable:
        return cfg
    try:
        idp = from_discovery(http or UrllibHttp(), cfg.idp.issuer or cfg.idp.name)
    except Exception as exc:  # noqa: BLE001 — still unreachable, whatever the reason
        raise OidcSignInError(UNREACHABLE, f"discovery failed: {type(exc).__name__}") from None
    return replace(cfg, idp=idp, unreachable=False)


class IssuerPreset:
    """What the gate knows about a recognized issuer: button kind (glyph),
    display brand, and the right default subject claim."""

    __slots__ = ("kind", "brand", "subject")

    def __init__(self, kind: str, brand: str, subject: str = "sub"):
        self.kind, self.brand, self.subject = kind, brand, subject


def issuer_preset(issuer: str) -> IssuerPreset | None:
    """Recognize a well-known OIDC issuer URL so popular IdPs get the right
    glyph, label, and subject claim with zero extra configuration. Unknown
    issuers still work — they just wear the generic mark."""
    parts = urlsplit(issuer)
    host = (parts.hostname or "").lower()
    if host == "accounts.google.com":
        return IssuerPreset("google", "Google")
    if host in ("login.microsoftonline.com", "sts.windows.net"):
        return IssuerPreset("entra", "Microsoft", subject="oid")
    if host.startswith("cognito-idp.") and host.endswith(".amazonaws.com"):
        return IssuerPreset("aws", "AWS")
    if host.endswith(".cloudflareaccess.com"):
        return IssuerPreset("cloudflare", "Cloudflare")
    if host.endswith((".okta.com", ".oktapreview.com", ".okta-emea.com")):
        return IssuerPreset("okta", "Okta")
    if host.endswith(".auth0.com"):
        return IssuerPreset("auth0", "Auth0")
    if host in ("gitlab.com",) or host.startswith("gitlab."):
        return IssuerPreset("gitlab", "GitLab")
    if "/realms/" in parts.path:
        return IssuerPreset("keycloak", "Keycloak")
    return None


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _secret_from_vault(name: str, setting: str) -> str:
    """The client secret stored in the vault under ``name``.

    A name that resolves to nothing is refused, not ignored: ignoring it would
    quietly turn a confidential client into a public one, and the failure would
    surface at Microsoft's end as an unexplained sign-in error.
    """
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.paths import get_user_state_dir

    try:
        with ForeignCredentialStore(get_user_state_dir()).get(name) as secret:
            value = secret.as_str().strip()
    except KeyError:
        raise ValueError(f"{setting}={name!r} names no entry in the vault") from None
    if not value:
        raise ValueError(f"{setting}={name!r} is empty in the vault")
    return value


def providers_from_env(
    env: Mapping[str, str] | None = None, *, http: Any = None
) -> list[OidcSignIn]:
    """Every configured sign-in provider, in display order.

    The primary ``AXIOM_GATE_OIDC_*`` block (name ``"sso"`` — the institution
    button, on the historical routes) comes first when set.
    ``AXIOM_GATE_OIDC_PROVIDERS`` then names additional blocks: for example
    ``google,microsoft`` reads ``AXIOM_GATE_OIDC_GOOGLE_*`` and
    ``AXIOM_GATE_OIDC_MICROSOFT_*`` — each block the same keys as the primary
    (PROVIDER, CLIENT_ID, CLIENT_SECRET[_FILE], TENANT, LABEL, JIT,
    SUBJECT_CLAIM, ROLES_CLAIM, SCOPES, REDIRECT_PATH). A named block with
    nothing set is skipped with a warning; a half-configured one raises, the
    same as the primary. ``sso`` is reserved for the primary block."""
    e = os.environ if env is None else env
    out: list[OidcSignIn] = []
    primary = OidcSignIn.from_env(e, http=http)
    if primary is not None:
        out.append(primary)
    for raw in (e.get("AXIOM_GATE_OIDC_PROVIDERS") or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name == "sso" or not _NAME_RE.match(name):
            raise ValueError(f"invalid OIDC provider name {name!r} in AXIOM_GATE_OIDC_PROVIDERS")
        cfg = OidcSignIn._one_from_env(
            e, prefix=f"AXIOM_GATE_OIDC_{name.upper()}_", name=name, http=http
        )
        if cfg is None:
            _LOG.warning(
                "AXIOM_GATE_OIDC_PROVIDERS names %r but AXIOM_GATE_OIDC_%s_* is not set",
                name,
                name.upper(),
            )
            continue
        out.append(cfg)
    return out


def issue_txn(
    *,
    state: str,
    nonce: str,
    verifier: str,
    next_target: str,
    remember: bool,
    issuer: str,
    ttl: timedelta,
    max_age: int | None = None,
) -> str:
    """Sign the first-leg state into a token only this node can read back."""
    claims = {
        "sub": "oidc-txn",
        "type": _TXN_TYPE,
        "state": state,
        "nonce": nonce,
        "cv": verifier,
        "next": next_target,
        "remember": bool(remember),
    }
    if max_age is not None:
        claims["max_age"] = int(max_age)
    return create_access_token(
        claims,
        expires_delta=ttl,
        issuer=issuer,
    )


def verify_txn(token: str | None, *, issuer: str) -> dict | None:
    """The transaction claims, or ``None`` — a session or access token presented
    here is refused by type, so nothing else this node signs can stand in."""
    if not token:
        return None
    claims = verify_token(token, issuer=issuer)
    if claims is None or claims.get("type") != _TXN_TYPE:
        return None
    return claims


#: Longest re-authentication window a caller may ask for, in seconds.
MAX_REAUTH_AGE = 3600
#: Clock skew tolerated between the IdP and this node when checking auth_time.
AUTH_TIME_SKEW = 60


def begin(
    cfg: OidcSignIn,
    *,
    base_url: str,
    next_target: str,
    remember: bool,
    issuer: str,
    max_age: int | None = None,
) -> tuple[str, str]:
    """First leg. Returns ``(authorization_url, txn_token)``.

    With ``max_age`` the IdP is asked to sign the person in again
    (``prompt=login``) and to report when (``auth_time``); the second leg
    refuses an ID token that is older (ADR-146 re-authentication)."""
    if max_age is not None and not 0 <= max_age <= MAX_REAUTH_AGE:
        raise ValueError(f"max_age must be 0..{MAX_REAUTH_AGE} seconds")
    if cfg.unreachable:
        raise OidcSignInError(UNREACHABLE, "the issuer's discovery document was never read")
    extra = dict(cfg.extra_authorize_params or {})
    if max_age is not None:
        extra.update({"prompt": "login", "max_age": str(max_age)})
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    url = authorization_url(
        cfg.idp,
        client_id=cfg.client_id,
        redirect_uri=cfg.redirect_uri(base_url),
        scopes=list(cfg.scopes),
        state=state,
        code_challenge=challenge,
        nonce=nonce,
        extra=extra or None,
    )
    txn = issue_txn(
        state=state,
        nonce=nonce,
        verifier=verifier,
        next_target=next_target,
        remember=remember,
        issuer=issuer,
        ttl=cfg.txn_ttl,
        max_age=max_age,
    )
    return url, txn


# ---------------------------------------------------------------- second leg


#: Microsoft's codes for "the redirect is not registered" and "this client
#: must send a secret": each has a fix only the site's administrator can make.
_REDIRECT_CODES = ("AADSTS50011", "AADSTS500113")
_SECRET_CODES = ("AADSTS7000218", "AADSTS700025")


def explain_idp_error(
    cfg: OidcSignIn, error: str, description: str, *, base_url: str = ""
) -> str | None:
    """A sentence naming the fix for a provider error, or ``None`` for one we
    cannot name a fix for. Never echoes the provider's text (it can carry ids)."""
    text = f"{error} {description}"
    lowered = text.lower()
    redirect = cfg.registered_redirect(base_url) if cfg.public else cfg.redirect_path
    if any(code in text for code in _REDIRECT_CODES) or "redirect_uri_mismatch" in lowered or (
        "redirect" in lowered and ("not registered" in lowered or "mismatch" in lowered
                                   or "does not match" in lowered)
    ):
        return (
            f"The site's app registration needs the loopback redirect {redirect}; "
            "ask the site admin."
        )
    if cfg.public and (
        any(code in text for code in _SECRET_CODES)
        or ("client_secret" in lowered or "client_assertion" in lowered)
    ):
        return (
            f"The site's app registration does not treat {redirect} as a public client "
            "(it asks for a secret); ask the site admin to list it under the mobile "
            "and desktop applications platform."
        )
    return None


_ADDRESS_CLAIMS = ("email", "preferred_username", "upn")


def _addresses(claims: Mapping[str, Any]) -> list[str]:
    """Every address the token carries for the person, mail address first.

    An institution often gives one person a sign-in name (``upn`` /
    ``preferred_username``) and a different mail address (``email``); both
    name the same account."""
    out: list[str] = []
    for key in _ADDRESS_CLAIMS:
        value = claims.get(key)
        if isinstance(value, str) and "@" in value:
            address = value.strip().lower()
            if address not in out:
                out.append(address)
    return out


def _pick_email(claims: Mapping[str, Any]) -> str:
    found = _addresses(claims)
    if found:
        return found[0]
    raise OidcSignInError(
        "Your sign-in did not include an email address.", "no email/preferred_username/upn claim"
    )


def _find_account(store: UserStore, subject: str, addresses: list[str]) -> User | None:
    """By immutable subject, then by any of the person's addresses (as email or
    as account id: ``dev up`` keys its accounts on the email)."""
    found = store.get_by_id(subject)
    if found is not None:
        return found
    for address in addresses:
        found = store.get_by_email(address) or store.get_by_id(address)
        if found is not None:
            return found
    return None


def resolve_account(cfg: OidcSignIn, store: UserStore, claims: Mapping[str, Any]) -> User:
    """Turn verified claims into the account that gets the session.

    Lookup order: by immutable subject, then by email (linking a pre-existing
    password account), then just-in-time creation when permitted. Disabled
    accounts are refused on every path. Roles: the IdP's claim wins when it is
    present; otherwise locally-assigned roles are kept.
    """
    subject = claims.get(cfg.subject_claim) or claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OidcSignInError(
            "Your sign-in did not identify you.", f"no {cfg.subject_claim!r}/sub claim"
        )
    email = _pick_email(claims)
    addresses = _addresses(claims)
    name = str(claims.get("name") or "")
    claimed_roles = claims.get(cfg.roles_claim) if cfg.idp_roles else None
    has_roles_claim = isinstance(claimed_roles, list)
    roles = tuple(str(r) for r in claimed_roles) if has_roles_claim else None
    provenance: dict[str, Any] = {"idp": cfg.idp.name, "idp_subject": subject}
    if len(addresses) > 1:
        provenance["aliases"] = addresses

    existing = _find_account(store, subject, addresses)
    if existing is not None:
        if existing.disabled:
            raise OidcSignInError("This account is disabled.", f"disabled: {existing.user_id}")
        known = existing.attributes.get("aliases")
        if isinstance(known, list) and len(addresses) > 1:
            provenance["aliases"] = list(dict.fromkeys([*known, *addresses]))
        # A local development node (a public client) signs the person in as
        # the principal the site knows: keyed on the IdP subject, exactly as
        # the site's own just-in-time account is. A local account found under
        # one of their addresses (the password account `dev up` made) is
        # re-keyed to that subject, keeping its password, roles and address.
        rekey = cfg.public and cfg.account_id == "subject" and existing.user_id != subject
        merged = User(
            user_id=subject if rekey else existing.user_id,
            # An account found under one of the person's addresses keeps it, so
            # a password sign-in under that address still finds it.
            email=existing.email if existing.email in addresses else email,
            password_hash=existing.password_hash,
            name=name or existing.name,
            roles=roles if roles is not None else existing.roles,
            disabled=False,
            attributes={**existing.attributes, **provenance},
            site=existing.site,
        )
        if merged == existing:
            return existing
        return _write(store, merged) or existing

    if not cfg.jit:
        raise OidcSignInError(
            "Your account has not been provisioned yet.", f"jit disabled; unknown subject {subject}"
        )
    if not roles:
        # No roles claim from the IdP → the configured JIT default role
        # ($AXIOM_GATE_JIT_ROLE, default "viewer"), so a first sign-in never
        # lands in a role-less limbo the consumer layer cannot reason about.
        from .role_bundles import jit_default_role

        default_role = jit_default_role()
        roles = (default_role,) if default_role else ()
    created = User(
        user_id=email if cfg.account_id == "email" else subject,
        email=email,
        password_hash=None,
        name=name,
        roles=roles,
        attributes=provenance,
    )
    written = _write(store, created)
    if written is None:
        raise OidcSignInError(
            "Accounts cannot be created on this node.",
            "user store is not writable; set AXIOM_GATE_OIDC_JIT=0 or provision the account",
        )
    return written


def _write(store: UserStore, user: User) -> User | None:
    if isinstance(store, WritableUserStore):
        return store.upsert(user)
    _LOG.warning(
        "gate user store %s is read-only — SSO account state not persisted", type(store).__name__
    )
    return None


def complete(
    cfg: OidcSignIn,
    *,
    http: Any,
    jwks: JwksCache,
    store: UserStore,
    base_url: str,
    txn: Mapping[str, Any],
    code: str,
    state: str,
    now: float | None = None,
) -> tuple[User, Mapping[str, Any]]:
    """Second leg: code → tokens → verified id_token → ``(account, id_token claims)``.

    The claims let the caller carry ``auth_time`` and ``amr`` into the session."""
    if not secrets.compare_digest(str(txn.get("state", "")), state):
        raise OidcSignInError(
            "The sign-in could not be verified. Please try again.", "state mismatch"
        )
    redirect_uri = cfg.redirect_uri(base_url)
    try:
        tokens = exchange_code(
            http,
            cfg.idp,
            client_id=cfg.client_id,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=str(txn.get("cv", "")),
            # A public client sends none, whatever the config object holds.
            client_secret=None if cfg.public else cfg.client_secret,
        )
    except IdpHttpError as exc:
        raise OidcSignInError(
            explain_idp_error(cfg, exc.error, exc.description, base_url=base_url)
            or "The sign-in provider did not accept the request.",
            f"token exchange failed: {exc!r} {exc.description[:200]!r}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — network / IdP error; never surfaced verbatim
        raise OidcSignInError(
            "The sign-in provider did not accept the request.", f"token exchange failed: {exc!r}"
        ) from exc
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if not isinstance(id_token, str) or not id_token:
        raise OidcSignInError(
            "The sign-in provider returned no identity.",
            f"no id_token in token response (keys={sorted(tokens) if isinstance(tokens, dict) else '?'})",
        )
    if not cfg.idp.issuer or not cfg.idp.jwks_uri:
        raise OidcSignInError(
            "This sign-in provider is not fully configured.", "idp has no issuer/jwks_uri"
        )
    try:
        claims = verify_id_token(
            id_token,
            jwks=jwks.get(_kid_of(id_token)),
            issuer=cfg.idp.issuer,
            audience=cfg.client_id,
            now=now,
        )
    except TokenVerificationError as exc:
        raise OidcSignInError(
            "The sign-in could not be verified. Please try again.", f"id_token rejected: {exc}"
        ) from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), str(txn.get("nonce", ""))):
        raise OidcSignInError(
            "The sign-in could not be verified. Please try again.", "nonce mismatch"
        )
    max_age = txn.get("max_age")
    if max_age is not None:
        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, int):
            raise OidcSignInError(
                "The sign-in provider did not confirm a fresh sign-in.",
                "max_age requested but the id_token has no auth_time",
            )
        age = (now if now is not None else time.time()) - auth_time
        if age > int(max_age) + AUTH_TIME_SKEW:
            raise OidcSignInError(
                "The sign-in provider did not sign you in again. Please try again.",
                f"auth_time is {int(age)}s old, max_age {max_age}",
            )
    return resolve_account(cfg, store, claims), claims


__all__ = [
    "DEFAULT_LABEL",
    "DEFAULT_SCOPES",
    "AUTH_TIME_SKEW",
    "DEFAULT_TXN_TTL",
    "MAX_REAUTH_AGE",
    "TXN_COOKIE",
    "CLIENT_TYPES",
    "IdpHttpError",
    "JwksCache",
    "LOOPBACK_REDIRECTS",
    "OidcSignIn",
    "OidcSignInError",
    "UNREACHABLE",
    "UrllibHttp",
    "begin",
    "complete",
    "explain_idp_error",
    "issue_txn",
    "rediscover",
    "resolve_account",
    "verify_txn",
]
