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

from .protocol import SecretStoreProvider

_log = logging.getLogger(__name__)


class SecretStoreRegistry:
    """Process-global registry of ``SecretStoreProvider`` classes."""

    _providers: dict[str, type[SecretStoreProvider]] = {}

    @classmethod
    def register(cls, provider_cls: type[SecretStoreProvider]) -> None:
        """Register a provider class. ``provider_cls.kind`` is the key."""
        kind = provider_cls.kind
        if not kind:
            raise ValueError(
                f"{provider_cls.__name__} must set a non-empty `kind` class attr"
            )
        existing = cls._providers.get(kind)
        if existing is not None and existing is not provider_cls:
            raise ValueError(
                f"SecretStoreProvider kind {kind!r} already registered by "
                f"{existing.__module__}.{existing.__qualname__}; refusing to "
                f"clobber with {provider_cls.__module__}.{provider_cls.__qualname__}"
            )
        cls._providers[kind] = provider_cls
        _log.debug("Registered SecretStoreProvider kind=%s", kind)

    @classmethod
    def unregister(cls, kind: str) -> None:
        """Remove a provider; used by tests cleaning up after themselves."""
        cls._providers.pop(kind, None)

    @classmethod
    def available_kinds(cls) -> list[str]:
        return sorted(cls._providers)

    @classmethod
    def get(cls, kind: str) -> type[SecretStoreProvider]:
        try:
            return cls._providers[kind]
        except KeyError as exc:
            known = ", ".join(cls.available_kinds()) or "(none registered)"
            raise KeyError(
                f"No SecretStoreProvider registered for kind={kind!r}. "
                f"Known kinds: {known}"
            ) from exc

    @classmethod
    def create(cls, kind: str, config: dict[str, Any]) -> SecretStoreProvider:
        """Factory entrypoint. Instantiates the registered provider class."""
        provider_cls = cls.get(kind)
        return provider_cls(config)
