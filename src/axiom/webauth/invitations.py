# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Invitations: somebody mints their own key, nobody mints it for them.

Onboarding three colleagues on 2026-10-01 took this shape. Each one needed a
scoped key, so the administrator logged into the node, ran ``gate issue api-key``
three times, and sent each person their key. Every key therefore existed in the
administrator's terminal, in the administrator's scrollback, and in whatever
channel carried it. One reached a chat paste and had to be rotated.

The administrator never needed to see any of them. What an administrator decides
is **who may have what**. The secret itself only has to reach one machine, and it
is not theirs.

So: an administrator creates an invitation naming a principal, a role and a site.
The colleague redeems it, and the key is minted on their side. What travels is
not a credential for anything except its own redemption, it is single-use, and it
expires.

**An invitation can only shrink.** Scopes are recorded when it is created,
resolved again when it is redeemed, and a redemption that would grant more than
was approved is refused. A role widened in between does not widen invitations
nobody approved at that width. A role narrowed in between does narrow them, which
is the direction that is safe to apply without asking. That rule is what makes an
unredeemed invitation safe to leave outstanding.

Mirrors :mod:`axiom.webauth.api_keys` deliberately: the same prefix-and-id token
shape, the same scrypt hash at rest, the same fail-closed file handling. Two
credential stores with different habits is how one of them ends up weaker.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.webauth.password import get_password_hash, verify_password

from .api_keys import bind_principal_to_site, mint_api_key

__all__ = [
    "INVITATION_PREFIX",
    "InvitationRefused",
    "InvitationsFileError",
    "append_invitation",
    "expired",
    "load_invitations",
    "mint_invitation",
    "outstanding",
    "parse_code",
    "redeem_invitation",
    "revoke_invitation",
    "save_invitations",
]

#: Distinct from the key prefix on purpose. Somebody holding one of these should
#: be able to tell at a glance that it is not yet a key, and a leak report should
#: not have to guess which kind of secret was pasted.
INVITATION_PREFIX = "axi_inv_"

#: What `code_hash` and the other at-rest fields are, so a listing can strip
#: them without keeping a second list that drifts.
_SECRET_FIELDS = ("code_hash",)


class InvitationsFileError(ValueError):
    """The invitations file could not be read as invitations.

    Raised rather than treated as empty. An unreadable file read as "no
    invitations" would refuse every redemption with the wrong reason, and
    written back as empty would discard everybody else's.
    """


class InvitationRefused(ValueError):
    """This invitation cannot be redeemed, and the message says which reason.

    Specific rather than uniform. The code carries 32 bytes of entropy so there
    is nothing to enumerate, and a colleague who cannot tell "already used" from
    "expired" from "wrong code" has to go and ask somebody — which is the dead
    end this whole surface exists to remove.
    """


def parse_code(code: str) -> tuple[str, str] | None:
    """Split a code into ``(invitation_id, secret)``, or ``None`` if malformed."""
    if not isinstance(code, str) or not code.startswith(INVITATION_PREFIX):
        return None
    rest = code[len(INVITATION_PREFIX) :]
    invitation_id, _, secret = rest.partition("_")
    if not invitation_id or not secret:
        return None
    return invitation_id, secret


def mint_invitation(
    *,
    principal: str,
    roles: Sequence[str],
    scopes: Sequence[str],
    site: str | None,
    name: str = "",
    invited_by: str = "",
    expires_in: timedelta | None,
    now: datetime | None = None,
) -> tuple[str, dict]:
    """Create an invitation: returns ``(plaintext_code, record)``.

    ``scopes`` is the ceiling — what the administrator approved, resolved from
    ``roles`` at this moment. Redemption resolves the roles again and refuses to
    exceed this.

    ``expires_in`` is required. A credential with no end is one nobody remembers
    to revoke, and an invitation is the easiest kind to forget because it does
    nothing until somebody uses it.
    """
    if expires_in is None:
        raise ValueError(
            "an invitation needs an expiry: pass expires_in. An invitation with "
            "no end is one nobody remembers to revoke"
        )
    if not roles and not scopes:
        raise ValueError("an invitation must grant something: pass roles or scopes")
    handle, bound_site = bind_principal_to_site(principal, site)
    at = now or datetime.now(UTC)
    invitation_id = uuid.uuid4().hex[:12]
    secret = secrets.token_urlsafe(32)
    code = f"{INVITATION_PREFIX}{invitation_id}_{secret}"
    record = {
        "invitation_id": invitation_id,
        "principal": handle,
        "roles": [str(r) for r in roles],
        # The ceiling, resolved when the administrator approved it.
        "scopes": [str(s) for s in scopes],
        "site": bound_site,
        "name": str(name or ""),
        "invited_by": str(invited_by or ""),
        "code_hash": get_password_hash(code),
        "created_at": at.isoformat(timespec="seconds"),
        # An instant, not a duration: a duration would restart every time the
        # record was read.
        "expires_at": (at + expires_in).isoformat(timespec="seconds"),
        "redeemed_at": None,
        "revoked_at": None,
        "key_id": None,
    }
    return code, record


# ---------------------------------------------------------------------------
# File I/O — fail-closed and atomic, mirroring api_keys.py
# ---------------------------------------------------------------------------


def _validate(raw: object, *, where: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvitationsFileError(f"{where}: expected a list of invitations")
    out: list[dict] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise InvitationsFileError(f"{where}: entry {i} is not an object")
        if not row.get("invitation_id"):
            raise InvitationsFileError(f"{where}: entry {i} has no invitation_id")
        out.append(row)
    return out


def load_invitations(path: str | os.PathLike) -> list[dict]:
    """Every invitation on file. An absent file is empty, not an error."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvitationsFileError(f"{p}: not valid JSON ({exc})") from exc
    return _validate(raw, where=str(p))


def save_invitations(path: str | os.PathLike, records: Iterable[dict]) -> Path:
    """Write the whole set atomically, 0600."""
    p = Path(path)
    rows = _validate(list(records), where=str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    return p


def append_invitation(path: str | os.PathLike, record: dict) -> dict:
    rows = load_invitations(path)
    rows.append(record)
    save_invitations(path, rows)
    return record


def revoke_invitation(path: str | os.PathLike, invitation_id: str) -> dict:
    rows = load_invitations(path)
    for row in rows:
        if row.get("invitation_id") == invitation_id:
            row["revoked_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            save_invitations(path, rows)
            return row
    raise InvitationRefused(f"no invitation with id {invitation_id!r}")


# ---------------------------------------------------------------------------
# Listing — for the administrator who has to clean up
# ---------------------------------------------------------------------------


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _SECRET_FIELDS}


def _is_expired(row: dict, *, now: datetime | None = None) -> bool:
    try:
        return datetime.fromisoformat(str(row.get("expires_at"))) <= (now or datetime.now(UTC))
    except (TypeError, ValueError):
        # An unparseable expiry is treated as expired. Fail closed: the
        # alternative is an invitation that never dies.
        return True


def outstanding(path: str | os.PathLike, *, now: datetime | None = None) -> list[dict]:
    """Invitations still waiting to be redeemed, without their stored hashes."""
    return [
        _public(r)
        for r in load_invitations(path)
        if not r.get("redeemed_at") and not r.get("revoked_at") and not _is_expired(r, now=now)
    ]


def expired(path: str | os.PathLike, *, now: datetime | None = None) -> list[dict]:
    """Invitations nobody used in time.

    Separate from :func:`outstanding` because an administrator reading a list
    needs to tell "still waiting on them" from "they missed it and need another".
    """
    return [
        _public(r)
        for r in load_invitations(path)
        if not r.get("redeemed_at") and not r.get("revoked_at") and _is_expired(r, now=now)
    ]


# ---------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------


def redeem_invitation(
    path: str | os.PathLike,
    code: str,
    *,
    resolve_roles: Callable[[Sequence[str]], Sequence[str]],
    now: datetime | None = None,
) -> tuple[str, dict, dict]:
    """Spend an invitation and mint its key. Returns ``(token, key, invitation)``.

    ``resolve_roles`` turns the invitation's role names into scopes, now. It is
    injected because resolving a role means reading the registered bundles, and
    what this function owns is the decision, not where bundles live.

    Refuses, and writes nothing, when the code is unknown or wrong, when the
    invitation is spent, revoked or expired, when a named role no longer exists,
    or when the roles now resolve to more than was approved.
    """
    parsed = parse_code(code)
    if parsed is None:
        raise InvitationRefused(
            "that is not an invitation code — it should begin "
            f"{INVITATION_PREFIX!r} and carry an id and a secret"
        )
    invitation_id, _secret = parsed

    rows = load_invitations(path)
    row = next((r for r in rows if r.get("invitation_id") == invitation_id), None)
    if row is None or not verify_password(code, str(row.get("code_hash") or "")):
        # One message for both, because they are the same thing to the holder: a
        # code that does not open anything. Distinguishing them would leak which
        # ids exist for no benefit to anybody legitimate.
        raise InvitationRefused("that invitation code is not recognised")

    if row.get("revoked_at"):
        raise InvitationRefused(
            f"that invitation was revoked on {row['revoked_at']} — ask for a new one"
        )
    if row.get("redeemed_at"):
        raise InvitationRefused(
            f"that invitation was already redeemed on {row['redeemed_at']}. "
            "An invitation is single-use; ask for a new one"
        )
    if _is_expired(row, now=now):
        raise InvitationRefused(
            f"that invitation expired on {row.get('expires_at')} — ask for a new one"
        )

    approved = tuple(str(s) for s in (row.get("scopes") or ()))
    roles = tuple(str(r) for r in (row.get("roles") or ()))
    if roles:
        try:
            now_scopes = tuple(str(s) for s in resolve_roles(roles))
        except KeyError as exc:
            raise InvitationRefused(
                f"this invitation names the role {exc.args[0]!r}, which no longer "
                f"exists on this node. Ask for a new invitation"
            ) from exc
        extra = [s for s in now_scopes if s not in approved]
        if extra:
            raise InvitationRefused(
                f"the role(s) {', '.join(roles)} now grant more than this "
                f"invitation approved: {', '.join(extra)}. An invitation can only "
                f"shrink, so this one is refused rather than widened — ask for a "
                f"new one"
            )
        granted = tuple(s for s in approved if s in now_scopes)
    else:
        granted = approved

    if not granted:
        raise InvitationRefused(
            "this invitation would grant nothing — its role(s) no longer overlap "
            "what it approved. Ask for a new one"
        )

    token, key_record = mint_api_key(
        principal=str(row.get("principal") or ""),
        scopes=granted,
        name=str(row.get("name") or "") or f"redeemed invitation {invitation_id}",
        site=row.get("site"),
    )

    at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    row["redeemed_at"] = at
    row["key_id"] = key_record["key_id"]
    save_invitations(path, rows)
    return token, key_record, row
