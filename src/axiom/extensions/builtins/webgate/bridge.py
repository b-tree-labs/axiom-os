# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The OIDC bridge — one login for cookie-session and OAuth/OIDC alike (ADR-003).

The webgate login sets a ``webauth`` session cookie. This adapter turns that
cookie into the ``SubjectResolver`` the ``oauth`` authorization server expects,
so an already-logged-in browser passes straight through ``/oauth/authorize``
without a second login. Wiring the fast-follow is one line at startup::

    from axiom.extensions.builtins.oauth import set_subject_resolver
    from axiom.extensions.builtins.webgate.bridge import session_subject_resolver
    set_subject_resolver(session_subject_resolver)

Trust is the ES256 signature (verified from the JWKS), so the resolver does not
pin an issuer — any validly-signed session this node minted is accepted.
"""

from __future__ import annotations

from fastapi import Request

from axiom.webauth import session_from_cookies


def session_subject_resolver(request: Request) -> str | None:
    """Resolve the OAuth resource owner from the webgate session cookie."""
    claims = session_from_cookies(request.cookies)
    if not claims:
        return None
    return claims.get("sub")


__all__ = ["session_subject_resolver"]
