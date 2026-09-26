# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RotationRegistry — ``kind`` → ``RotationStrategy`` class map.

Instance-based (unlike the process-global ``SecretStoreRegistry``): an
install builds one registry from its configured rotation strategies, so
two installs — or a test and production — never share strategy state. A
module-level ``default_registry()`` is provided for the common single-
registry case where builtin strategies self-register.
"""

from __future__ import annotations

import logging

from .strategy import RotationStrategy

_log = logging.getLogger(__name__)


class RotationRegistry:
    """A set of ``RotationStrategy`` classes keyed by ``kind``."""

    def __init__(self) -> None:
        self._strategies: dict[str, type[RotationStrategy]] = {}

    def register(self, strategy_cls: type[RotationStrategy]) -> None:
        kind = getattr(strategy_cls, "kind", "")
        if not kind:
            raise ValueError(
                f"{strategy_cls.__name__} must set a non-empty `kind` class attr"
            )
        existing = self._strategies.get(kind)
        if existing is not None and existing is not strategy_cls:
            raise ValueError(
                f"RotationStrategy kind {kind!r} already registered by "
                f"{existing.__module__}.{existing.__qualname__}"
            )
        self._strategies[kind] = strategy_cls
        _log.debug("Registered RotationStrategy kind=%s", kind)

    def unregister(self, kind: str) -> None:
        self._strategies.pop(kind, None)

    def available_kinds(self) -> list[str]:
        return sorted(self._strategies)

    def get(self, kind: str) -> type[RotationStrategy]:
        try:
            return self._strategies[kind]
        except KeyError as exc:
            known = ", ".join(self.available_kinds()) or "(none registered)"
            raise KeyError(
                f"No RotationStrategy registered for kind={kind!r}. "
                f"Known kinds: {known}"
            ) from exc


_DEFAULT: RotationRegistry | None = None


def default_registry() -> RotationRegistry:
    """The process-wide default registry (lazy singleton)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = RotationRegistry()
    return _DEFAULT


__all__ = ["RotationRegistry", "default_registry"]
