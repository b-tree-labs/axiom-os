# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Directory adapters + their registry — ADR-103 decisions 2 and 3.

Day one: ``entra`` (Microsoft Graph), ``oidc_claims`` (straight from verified
token claims, zero extra calls), and ``local`` (file-backed). ``local`` is not a
test double — ADR-022 requires identity to work with no external authority
present, so an air-gapped or single-operator node has a working directory with
no network dependency at all.

Registry mirrors ``auth``'s provider registry deliberately: one place to add a
source, and nothing above this module knows which authority answered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator

from axiom.extensions.builtins.directory.protocol import (
    DeltaPage,
    DirectoryCapability,
    Group,
    GroupRef,
    PrincipalRef,
)
from axiom.infra.connector_registry import ConnectorRegistry


class _BaseDirectory:
    """Shared refusals so each adapter declares only what it can do."""

    name = "base"
    capabilities: frozenset[DirectoryCapability] = frozenset()

    def groups_for(self, principal: PrincipalRef) -> list[GroupRef]:
        raise NotImplementedError(f"{self.name}: LOOKUP not supported")

    def resolve_group(self, ref: GroupRef) -> Group | None:
        return None

    def members_of(self, ref: GroupRef) -> Iterator[PrincipalRef]:
        raise NotImplementedError(
            f"{self.name}: ENUMERATE not supported — negotiate on .capabilities"
        )

    def delta(self, since: str | None = None) -> DeltaPage:
        raise NotImplementedError(f"{self.name}: DELTA not supported — negotiate on .capabilities")


class OidcClaimsDirectory(_BaseDirectory):
    """Groups straight off a verified token. No network, no enumeration.

    The right default when an IdP emits complete group claims; useless when it
    does not, which is exactly why capability negotiation exists.
    """

    name = "oidc_claims"
    capabilities = frozenset({DirectoryCapability.LOOKUP})

    def __init__(self, *, claims: dict | None = None) -> None:
        self._claims = claims or {}

    def groups_for(self, principal: PrincipalRef) -> list[GroupRef]:
        return [
            GroupRef(id=str(g), provider=self.name) for g in self._claims.get("groups", []) or []
        ]


class LocalDirectory(_BaseDirectory):
    """File- or dict-backed directory. The no-external-authority floor.

    File shape::

        {"groups": {"g-ops": {"members": ["oid-111"], "display": "Operators"}}}

    The in-memory form (``groups={"g-ops": ["oid-111"]}``) is the same data
    without the envelope, so tests and small deployments share one code path.
    """

    name = "local"
    capabilities = frozenset({DirectoryCapability.LOOKUP, DirectoryCapability.ENUMERATE})

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        groups: dict[str, Any] | None = None,
    ) -> None:
        raw: dict[str, Any] = {}
        if path is not None:
            raw = json.loads(Path(path).read_text()).get("groups", {})
        elif groups is not None:
            raw = groups
        self._groups: dict[str, dict[str, Any]] = {
            gid: (spec if isinstance(spec, dict) else {"members": list(spec)})
            for gid, spec in raw.items()
        }

    def groups_for(self, principal: PrincipalRef) -> list[GroupRef]:
        return [
            GroupRef(id=gid, provider=self.name, display=spec.get("display"))
            for gid, spec in self._groups.items()
            if principal.subject in (spec.get("members") or [])
        ]

    def resolve_group(self, ref: GroupRef) -> Group | None:
        spec = self._groups.get(ref.id)
        return None if spec is None else Group(ref=ref, display_name=spec.get("display"))

    def members_of(self, ref: GroupRef) -> Iterator[PrincipalRef]:
        for subject in self._groups.get(ref.id, {}).get("members") or []:
            yield PrincipalRef(subject=str(subject), provider=self.name)


class EntraDirectory(_BaseDirectory):
    """Microsoft Graph.

    The Graph client is **injected** — an object with
    ``get(path) -> dict`` — so mapping and pagination are unit-tested against a
    fake with no credentials and no network, matching the calendar vendors.

    Handles the claims-overage case by construction: this adapter exists
    precisely because a token cannot always carry the group list.
    """

    name = "entra"
    capabilities = frozenset(
        {
            DirectoryCapability.LOOKUP,
            DirectoryCapability.ENUMERATE,
            DirectoryCapability.DELTA,
        }
    )

    def __init__(self, *, client: Any, page_limit: int = 50) -> None:
        self._client = client
        self._page_limit = page_limit

    def _walk(self, path: str) -> Iterator[dict]:
        """Follow ``@odata.nextLink`` — group lists routinely exceed one page."""
        pages = 0
        while path and pages < self._page_limit:
            body = self._client.get(path)
            pages += 1
            yield from body.get("value", []) or []
            path = body.get("@odata.nextLink") or ""

    def groups_for(self, principal: PrincipalRef) -> list[GroupRef]:
        path = f"/users/{principal.subject}/memberOf?$select=id,displayName&$top=999"
        return [
            GroupRef(id=str(o["id"]), provider=self.name, display=o.get("displayName"))
            for o in self._walk(path)
            if o.get("id")
        ]

    def resolve_group(self, ref: GroupRef) -> Group | None:
        body = self._client.get(f"/groups/{ref.id}?$select=id,displayName")
        if not body or not body.get("id"):
            return None
        return Group(ref=ref, display_name=body.get("displayName"))

    def members_of(self, ref: GroupRef) -> Iterator[PrincipalRef]:
        for o in self._walk(f"/groups/{ref.id}/members?$select=id&$top=999"):
            if o.get("id"):
                yield PrincipalRef(subject=str(o["id"]), provider=self.name)

    def delta(self, since: str | None = None) -> DeltaPage:
        body = self._client.get(since or "/groups/delta?$select=id,displayName")
        return DeltaPage(
            changes=tuple(body.get("value", []) or []),
            cursor=body.get("@odata.deltaLink") or body.get("@odata.nextLink"),
        )


# One shared registry mechanism (ADR-110 §Decision-1); this family is now a thin
# wrapper over it. UnknownConnectorKind is a ValueError, so get_provider keeps its
# original contract.
_registry: ConnectorRegistry = ConnectorRegistry("directory provider")
_registry.register("entra", EntraDirectory)
_registry.register("oidc_claims", OidcClaimsDirectory)
_registry.register("local", LocalDirectory)


def register_provider(name: str, factory: Callable[..., Any]) -> None:
    """Add a source (``scim``, ``ldap``, ``github``…) without touching callers."""
    _registry.register(name, factory, replace=True)


def available_providers() -> tuple[str, ...]:
    return _registry.available()


def get_provider(name: str, **config: Any):
    return _registry.create(name, **config)


__all__ = [
    "EntraDirectory",
    "LocalDirectory",
    "OidcClaimsDirectory",
    "available_providers",
    "get_provider",
    "register_provider",
]
