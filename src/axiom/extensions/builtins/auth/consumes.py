# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Resolve an AEOS ``[[extension.consumes]]`` credential declaration (AEOS-2,
spec-aeos-identity-addendum §2/§3).

An extension declares *what* it needs (an IdP token for some scopes, at some
posture floor); the runtime resolves it to *what it uses* (a ``token_source``
callable) — enforcing the posture floor first (AEOS-ID-2), so an under-assured
principal can't dereference a floored credential. The extension never touches
OAuth.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from axiom.extensions.builtins.auth.token_store import token_source
from axiom.infra.principal import PrincipalContext


class PostureError(Exception):
    """The acting principal does not meet a credential's ``min_posture`` floor."""


def _idp_for(decl: dict, tenant: Optional[str]):
    name = decl["idp"]
    if name == "entra":
        from axiom.extensions.builtins.auth.providers import entra

        t = tenant or decl.get("tenant")
        if not t:
            raise ValueError("entra credential requires a tenant id")
        return entra(t)
    if name == "google":
        from axiom.extensions.builtins.auth.providers import google

        return google()
    raise ValueError(f"unknown idp {name!r}")


def resolve_credential(
    decl: dict,
    *,
    principal: PrincipalContext,
    http: Any,
    client_id: str,
    client_secret: Optional[str] = None,
    store: Optional[Any] = None,
    tenant: Optional[str] = None,
    user: Optional[str] = None,
    now: Optional[Callable[[], float]] = None,
) -> Callable[[], str]:
    """Resolve a ``kind="credential"`` consumes block to a ``token_source``.

    Raises ``PostureError`` if ``principal`` is below the declared
    ``min_posture`` (default ``open``).
    """
    floor = decl.get("min_posture", "open")
    if not principal.meets(floor):
        raise PostureError(
            f"credential needs posture '{floor}'; principal '{principal.handle}' "
            f"is '{principal.posture}' — step up first"
        )
    return token_source(
        provider=_idp_for(decl, tenant),
        user=user or principal.handle,
        scopes=list(decl["scopes"]),
        http=http,
        client_id=client_id,
        client_secret=client_secret,
        store=store,
        now=now,
    )


__all__ = ["PostureError", "resolve_credential"]
