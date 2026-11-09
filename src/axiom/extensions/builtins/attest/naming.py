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
"""

from __future__ import annotations

import os
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


def signer_handle(claims: Mapping[str, Any]) -> str:
    """The person's handle. Raises ``ValueError`` when the session names no one."""
    sub = str(claims.get("sub") or "")
    if not sub:
        raise ValueError("the session names no subject")
    return principal_from_idp_subject(sub, signer_site(claims) or "")


__all__ = ["signer_handle", "signer_site"]
