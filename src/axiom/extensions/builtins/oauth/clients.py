# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Client registry for the oauth AS (ADR-082).

A ``ClientRegistry`` answers one question: *given a ``client_id``, what client is
this?* This cut ships an in-memory registry (config / test provisioning); a
Postgres-backed registry over ``axiom.infra.db.session_for("oauth")`` and an
``axi oauth client`` provisioning verb follow. The protocol keeps the token
endpoint agnostic to where clients live.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from .models import OAuthClient


@runtime_checkable
class ClientRegistry(Protocol):
    """Lookup of a registered client by id. Returns ``None`` when unknown."""

    def get(self, client_id: str) -> OAuthClient | None: ...


class InMemoryClientRegistry:
    """A dict-backed registry — config-provisioned clients and tests.

    Not durable: a process restart forgets everything not re-loaded. The
    Postgres-backed registry is the production path (see module docstring).
    """

    def __init__(self, clients: Iterable[OAuthClient] = ()) -> None:
        self._by_id: dict[str, OAuthClient] = {c.client_id: c for c in clients}

    def get(self, client_id: str) -> OAuthClient | None:
        return self._by_id.get(client_id)

    def add(self, client: OAuthClient) -> None:
        self._by_id[client.client_id] = client

    def __len__(self) -> int:
        return len(self._by_id)


_REGISTRY: ClientRegistry | None = None


def get_client_registry() -> ClientRegistry:
    """The process-wide client registry (cached).

    Defaults to an empty in-memory registry: with no clients provisioned the
    token endpoint fails every request closed (``invalid_client``), which is the
    safe default. Config / DB loading wires real clients in a later cut.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = InMemoryClientRegistry()
    return _REGISTRY


def set_client_registry(registry: ClientRegistry) -> None:
    """Install the process-wide registry (deployment wiring / tests)."""
    global _REGISTRY
    _REGISTRY = registry


def reset_client_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "ClientRegistry",
    "InMemoryClientRegistry",
    "get_client_registry",
    "reset_client_registry_for_tests",
    "set_client_registry",
]
