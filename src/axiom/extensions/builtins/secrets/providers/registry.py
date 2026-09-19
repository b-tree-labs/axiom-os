# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SecretStoreRegistry — ``kind`` → provider-class map.

Process-global, populated at import time by concrete provider modules.
Mirrors how ``SourceKindProvider`` / ``DatabaseKindProvider`` /
``VectorStoreProvider`` register elsewhere in the codebase.

Operators select a provider by ``kind`` (one of the registered keys)
plus a config dict. ``create()`` is the factory entrypoint that
``axi data install`` and the ``resolve`` skill both go through; tests
can swap a fake by registering it under a test-only kind.
"""

from __future__ import annotations

import logging
from typing import Any

from axiom.infra.connector_registry import ConnectorRegistry

from .protocol import SecretStoreProvider

_log = logging.getLogger(__name__)


class SecretStoreRegistry:
    """Process-global registry of ``SecretStoreProvider`` classes.

    A thin wrapper over the shared ``ConnectorRegistry`` (ADR-110 §Decision-1 /
    ADR-112 D2) — the same kind→factory mechanism directory/channels/editors/
    storage/data_platform use. Keeps the secrets-specific bits: the key is the
    provider's own ``.kind``, and ``get`` speaks the ``SecretStoreProvider``
    vocabulary in its error.
    """

    _reg: ConnectorRegistry = ConnectorRegistry("secret store")

    @classmethod
    def register(cls, provider_cls: type[SecretStoreProvider]) -> None:
        """Register a provider class. ``provider_cls.kind`` is the key."""
        kind = provider_cls.kind
        if not kind:
            raise ValueError(
                f"{provider_cls.__name__} must set a non-empty `kind` class attr"
            )
        # ConnectorRegistry is idempotent for the same class and raises ValueError
        # ('already registered') on a different class under a live kind.
        cls._reg.register(kind, provider_cls)
        _log.debug("Registered SecretStoreProvider kind=%s", kind)

    @classmethod
    def unregister(cls, kind: str) -> None:
        """Remove a provider; used by tests cleaning up after themselves."""
        cls._reg.unregister(kind)

    @classmethod
    def available_kinds(cls) -> list[str]:
        return list(cls._reg.available())

    @classmethod
    def get(cls, kind: str) -> type[SecretStoreProvider]:
        try:
            return cls._reg.get(kind)
        except KeyError:
            known = ", ".join(cls.available_kinds()) or "(none registered)"
            raise KeyError(
                f"No SecretStoreProvider registered for kind={kind!r}. "
                f"Known kinds: {known}"
            ) from None

    @classmethod
    def create(cls, kind: str, config: dict[str, Any]) -> SecretStoreProvider:
        """Factory entrypoint. Instantiates the registered provider class."""
        return cls._reg.create(kind, config)
