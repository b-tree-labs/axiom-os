# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How a signer is named, in one place.

The draft endpoint and the grant endpoint both name the person behind a
session, and a grant is only good for the person the draft is for. Two copies
of the rule would drift, so both call these.

A signer is ``@<subject>:<site>`` (ADR-020). The site is the session's own
``site`` claim. Accounts made before the gate stamped one have none, and
naming such a person without a context left them unable to hold any signing
role, so the site falls back to what the node itself says:

1. the session's ``site`` claim;
2. the one site the node is declared to serve (``AXIOM_SERVED_SITES``);
3. the node's own site (``AXIOM_SITE``) — but only when the node does not
   declare several, because choosing one of several would put a signature on
   a site the person did not sign for.

With none of those the handle has no context, as before, and a role grant
refuses it and says why.

The name is the subject's local part. A GitHub noreply address
(``12345+login@users.noreply.github.com``, which the contributor quickstart
recommends) is named by its login, and any other character ADR-020 does not
allow becomes ``-``. Only a subject that was refused before is changed, so no
existing handle moves. A subject that still yields no name raises
:class:`SignerNameError`, which says what is wrong with the email; it is not
the same thing as having no session.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from axiom.infra.principal import principal_from_idp_subject
from axiom.infra.site_scope import deployment_sites


def signer_site(claims: Mapping[str, Any]) -> str | None:
    """The site a session's person signs for, or None when nothing says."""
    claimed = str(claims.get("site") or "").strip()
    if claimed:
        return claimed
    served = deployment_sites()
    if served is not None:
        return next(iter(served)) if len(served) == 1 else None
    return (os.environ.get("AXIOM_SITE") or "").strip() or None


#: GitHub's private-email domain; its local part is ``<numeric id>+<login>``.
NOREPLY_DOMAIN = "users.noreply.github.com"

_NOREPLY_ID = re.compile(r"^\d+\+")
_NOT_A_NAME_CHAR = re.compile(r"[^A-Za-z0-9_.\-]+")


class SignerNameError(ValueError):
    """The session names a person, but their sign-in cannot be made a signer name."""


def signer_name(subject: str) -> str:
    """The ADR-020 name for a sign-in subject (an email or an IdP id)."""
    raw = (subject or "").strip()
    if raw.count("@") > 1:
        raise SignerNameError(
            f"the sign-in email {raw!r} cannot be a signer name: it has more than one '@'"
        )
    local, _, domain = raw.partition("@")
    if domain.lower() == NOREPLY_DOMAIN:
        local = _NOREPLY_ID.sub("", local)
    name = _NOT_A_NAME_CHAR.sub("-", local).strip("-.")
    if not name:
        raise SignerNameError(
            f"the sign-in email {raw!r} cannot be a signer name: the part before the '@' "
            "needs at least one letter or digit"
        )
    return name


def signer_handle(claims: Mapping[str, Any]) -> str:
    """The person's handle.

    Raises ``ValueError`` when the session names no one, and
    :class:`SignerNameError` (a ``ValueError``) when it names someone whose
    sign-in cannot be made a handle.
    """
    sub = str(claims.get("sub") or "")
    if not sub:
        raise ValueError("the session names no subject")
    name = signer_name(sub)
    try:
        return principal_from_idp_subject(name, signer_site(claims) or "")
    except ValueError as exc:
        raise SignerNameError(f"the sign-in {sub!r} cannot be a signer name: {exc}") from exc


__all__ = ["NOREPLY_DOMAIN", "SignerNameError", "signer_handle", "signer_name", "signer_site"]
