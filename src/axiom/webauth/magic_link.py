# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Magic-link sign-in — a single-use, expiring, email-verified credential.

The passwordless first-touch for external partner researchers (ACU/VCU/TAMU),
who are not in the UT tenant and so cannot use the Entra SSO gate. An access
admin invites an email *bound to a site* (the site is set here, server-side,
never taken from whoever clicks the link); issuance mints a high-entropy token
mailed to that address; redeeming it once yields the bound identity, which the
webgate turns into a session.

This module is the pure core: it does not send email and does not own durable
storage — a :class:`MagicLinkStore` is injected (in-memory here; Postgres via
``session_for("webauth")`` follows). It reuses the platform's one hashing scheme
(scrypt, :mod:`axiom.webauth.password`) so the token — like an API key — is
**hashed at rest**, and the same audit story covers both.

Audit properties: token hashed at rest, single-use (redeem marks it used),
short expiry (default 15 min — an email-borne bearer credential must be
short-lived even though the resulting *session* is generous), site-bound at
issuance, and constant-time secret verification.
"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from .password import get_password_hash, verify_password
from .users import _norm_email, validate_site

TOKEN_PREFIX = "aml_"

#: Default link lifetime — deliberately short. The link travels through email,
#: so it is the most exposed credential in the flow; the *session* it mints is
#: what's generous (30-day rolling), not the link.
DEFAULT_TTL_SECONDS = 15 * 60


class MagicLinkError(Exception):
    """A link could not be redeemed — unknown, malformed, expired, already used,
    or a bad secret. The user-facing surface should collapse all of these to one
    "this link is invalid or has expired" message; the distinct reasons are for
    the audit log, not the visitor."""


@dataclass(frozen=True)
class MagicLinkRecord:
    """A minted link, at rest. ``secret_hash`` is a scrypt hash — the plaintext
    secret is shown once (in the emailed URL) and never stored."""

    link_id: str
    email: str
    site: str
    secret_hash: str
    created_at: float
    expires_at: float
    roles: tuple[str, ...] = ()
    used_at: float | None = None


@dataclass(frozen=True)
class RedeemedLink:
    """The identity a redeemed link proves: a verified email, the site it was
    bound to at issuance, and any roles the invite carried."""

    email: str
    site: str
    roles: tuple[str, ...] = ()


@runtime_checkable
class MagicLinkStore(Protocol):
    """Durable home for minted links. Fail closed: ``get`` returns ``None`` for
    an unknown id rather than raising."""

    def put(self, record: MagicLinkRecord) -> None: ...
    def get(self, link_id: str) -> MagicLinkRecord | None: ...
    def mark_used(self, link_id: str, used_at: float) -> None: ...


class InMemoryMagicLinkStore:
    """A dict-backed store for tests + single-process dev. Postgres follows."""

    def __init__(self) -> None:
        self._by_id: dict[str, MagicLinkRecord] = {}

    def put(self, record: MagicLinkRecord) -> None:
        self._by_id[record.link_id] = record

    def get(self, link_id: str) -> MagicLinkRecord | None:
        return self._by_id.get(link_id)

    def mark_used(self, link_id: str, used_at: float) -> None:
        rec = self._by_id.get(link_id)
        if rec is not None:
            self._by_id[link_id] = replace(rec, used_at=used_at)


def split_magic_token(token: str) -> tuple[str, str] | None:
    """``aml_<link_id>_<secret>`` → ``(link_id, secret)``; ``None`` if malformed.

    Mirrors the API-key token grammar so resolution is O(1): find by ``link_id``,
    then one constant-time secret verify."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rest = token[len(TOKEN_PREFIX):]
    link_id, sep, secret = rest.partition("_")
    if not sep or not link_id or not secret:
        return None
    return link_id, secret


def issue_magic_link(
    email: str,
    site: str | None,
    *,
    roles: Iterable[str] = (),
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
    store: MagicLinkStore,
    link_id: str | None = None,
    secret: str | None = None,
) -> str:
    """Mint a single-use link for ``email`` bound to ``site`` and persist its
    hash. Returns the plaintext token to embed in the emailed URL — the only
    time the secret exists in the clear.

    ``site`` is required and validated: a link with no site is unattributable,
    and the site must come from the invite (server-side), never from the visitor
    who redeems it.
    """
    site_norm = validate_site(site)
    if not site_norm:
        raise ValueError("magic link requires a site (bound server-side at invite time)")
    email_norm = _norm_email(email)
    if not email_norm or "@" not in email_norm:
        raise ValueError(f"invalid email {email!r}")

    now = time.time() if now is None else now
    # hex id (no '_') so `aml_<id>_<secret>` splits unambiguously on the first
    # '_'; the secret may contain '_' (it is the remainder). Mirrors api_keys.
    link_id = link_id or uuid.uuid4().hex[:12]
    secret = secret or secrets.token_urlsafe(32)
    record = MagicLinkRecord(
        link_id=link_id,
        email=email_norm,
        site=site_norm,
        secret_hash=get_password_hash(secret),
        created_at=now,
        expires_at=now + float(ttl_seconds),
        roles=tuple(str(r) for r in roles),
    )
    store.put(record)
    return f"{TOKEN_PREFIX}{link_id}_{secret}"


def redeem_magic_link(
    token: str,
    *,
    now: float | None = None,
    store: MagicLinkStore,
) -> RedeemedLink:
    """Redeem a link exactly once. Raises :class:`MagicLinkError` on anything
    wrong — malformed, unknown, expired, already used, or a bad secret — after
    checking expiry/use so a valid-looking-but-dead link can't be probed for the
    secret. On success, marks the link used and returns the bound identity.
    """
    now = time.time() if now is None else now
    parts = split_magic_token(token)
    if parts is None:
        raise MagicLinkError("malformed magic-link token")
    link_id, secret = parts

    record = store.get(link_id)
    if record is None:
        raise MagicLinkError("unknown magic link")
    if record.used_at is not None:
        raise MagicLinkError("magic link already used")
    if now >= record.expires_at:
        raise MagicLinkError("magic link expired")
    if not verify_password(secret, record.secret_hash):
        raise MagicLinkError("magic link secret mismatch")

    store.mark_used(link_id, now)
    return RedeemedLink(email=record.email, site=record.site, roles=record.roles)


__all__ = [
    "TOKEN_PREFIX",
    "DEFAULT_TTL_SECONDS",
    "MagicLinkError",
    "MagicLinkRecord",
    "RedeemedLink",
    "MagicLinkStore",
    "InMemoryMagicLinkStore",
    "split_magic_token",
    "issue_magic_link",
    "redeem_magic_link",
]
