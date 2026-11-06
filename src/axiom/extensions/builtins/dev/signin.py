# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Find a sign-in client in the vault a local node can use.

A deployed node is told its OIDC client in its environment. A local one should
not need to be: if the person running it already holds an OIDC client
credential in their vault, recorded with the issuer it belongs to and its
client id, the node can offer that sign-in with nothing to configure.

Only the credential's *name* travels to the node. The gate reads the value out
of the vault itself (``AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT``), so the secret
never sits in the node's environment, where the process table would show it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

#: The environment block `oidc.OidcSignIn.from_env` reads.
PREFIX = "AXIOM_GATE_OIDC_"

#: Issuers whose tenant is part of the issuer URL. The gate has a preset for
#: this shape, which validates the tenant; a plain discovery URL would not.
_TENANT_ISSUERS = {"login.microsoftonline.com": "entra"}


@dataclass(frozen=True)
class SignInClient:
    credential: str
    issuer: str
    client_id: str
    provider: str
    tenant: str = ""

    def environment(self, *, label: str = "") -> dict[str, str]:
        env = {
            PREFIX + "PROVIDER": self.provider,
            PREFIX + "CLIENT_ID": self.client_id,
            PREFIX + "CLIENT_SECRET_VAULT": self.credential,
        }
        if self.tenant:
            env[PREFIX + "TENANT"] = self.tenant
        if label:
            env[PREFIX + "LABEL"] = label
        return env

    @property
    def label(self) -> str:
        if self.provider == "entra":
            return "Sign in with Microsoft"
        return f"Sign in with {urlsplit(self.issuer).hostname}"


def _client_from(meta: dict[str, Any]) -> SignInClient | None:
    issuer = str(meta.get("issuer_url") or "").strip()
    client_id = str(meta.get("client_id") or "").strip()
    name = str(meta.get("name") or "").strip()
    if not (issuer and client_id and name):
        return None
    parts = urlsplit(issuer)
    if parts.scheme != "https" or not parts.hostname:
        return None
    kind = _TENANT_ISSUERS.get(parts.hostname)
    if kind:
        tenant = next((p for p in parts.path.split("/") if p), "")
        if not tenant:
            return None
        return SignInClient(name, issuer, client_id, kind, tenant)
    return SignInClient(name, issuer, client_id, issuer)


def detect(entries: Iterable[dict[str, Any]]) -> tuple[SignInClient | None, list[str]]:
    """The one sign-in client the vault holds, and anything worth saying.

    More than one is not guessed between: the caller names the one it wants.
    """
    found = [c for c in (_client_from(m) for m in entries) if c is not None]
    if not found:
        return None, []
    if len(found) > 1:
        names = ", ".join(sorted(c.credential for c in found))
        return None, [
            f"the vault holds more than one sign-in client ({names}); "
            "name one with --sign-in <credential>"
        ]
    return found[0], []


def choose(entries: Iterable[dict[str, Any]], name: str) -> SignInClient | None:
    for meta in entries:
        if meta.get("name") == name:
            return _client_from(meta)
    return None


__all__ = ["PREFIX", "SignInClient", "choose", "detect"]
