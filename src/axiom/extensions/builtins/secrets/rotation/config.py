# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Vendor rotation config + the admin-client-from-cred-ref wiring (#16 / ADR-095).

A vendor-API rotation strategy needs an authenticated client, and that client's
credential must NOT be inlined — it lives in the SecretStore like any other
secret. ``RotationConfig`` names the vendor kind, the ref of its admin
credential, and the per-vendor knobs (base URL, managed name, scopes).
``build_vendor_strategy`` resolves the admin credential through the store seam,
builds a :class:`VendorHttpClient`, and constructs the concrete strategy.

This is the plumbing ``secrets.rotate`` was missing: with a config in hand,
``--strategy sendgrid`` runs end-to-end instead of returning "needs config".
New vendors (GitLab, LangSmith, Azure) land as additional ``kind`` branches on
this one factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..providers.protocol import SecretRef
from .strategies import RotationError, SendGridRotation
from .strategy import RotationStrategy
from .vendor_client import VendorHttpClient

# Per-vendor API base URLs. Overridable via ``RotationConfig.base_url`` for
# regional endpoints or test doubles.
_BASE_URLS: dict[str, str] = {
    "sendgrid": "https://api.sendgrid.com",
}


def _default_base_url(kind: str) -> str:
    """The vendor's default API base, or ``KeyError`` for an unknown kind."""
    return _BASE_URLS[kind]


@dataclass(frozen=True, slots=True)
class RotationConfig:
    """How to build a vendor-API rotation strategy for one secret.

    ``admin_ref`` is the SecretRef (as a string) of the credential that
    authorizes minting/revoking at the vendor — resolved through the store,
    never inlined. ``base_url`` defaults per kind; ``managed_name`` tags the
    credentials this strategy mints so revoke only ever touches our own.
    """

    kind: str
    admin_ref: str
    base_url: str | None = None
    managed_name: str | None = None
    scopes: tuple[str, ...] | None = None
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"


def _resolve_admin_token(admin_ref: str, store_for: Callable[[str], Any]) -> str:
    """Read the vendor admin credential from the store as a text token.

    Uses the ``Secret`` context manager so the value is zeroed after read.
    """
    ref = SecretRef.parse(admin_ref)
    store = store_for(ref.scheme)
    with store.get(ref) as secret:
        token = secret.value.decode("utf-8").strip()
    if not token:
        raise RotationError(
            f"admin credential {admin_ref} resolved empty; cannot build a "
            "vendor client without it"
        )
    return token


def build_vendor_strategy(
    config: RotationConfig, *, store_for: Callable[[str], Any]
) -> RotationStrategy:
    """Resolve the admin credential and build the concrete vendor strategy.

    Raises :class:`RotationError` when the admin credential is missing/empty
    and ``KeyError`` for an unknown vendor kind.
    """
    base_url = config.base_url or _default_base_url(config.kind)
    token = _resolve_admin_token(config.admin_ref, store_for)
    client = VendorHttpClient(
        base_url,
        token,
        auth_header=config.auth_header,
        auth_scheme=config.auth_scheme,
    )
    if config.kind == "sendgrid":
        return SendGridRotation(
            http=client,
            key_name=config.managed_name or "axiom-managed:sendgrid",
            scopes=list(config.scopes) if config.scopes else None,
        )
    raise KeyError(config.kind)


__all__ = ["RotationConfig", "build_vendor_strategy"]
