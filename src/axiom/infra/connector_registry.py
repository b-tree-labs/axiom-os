# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one registry mechanism every connector family shares (ADR-110 §Decision-1).

Secrets, directory, calendar, storage, and channels each re-derived the same
"``kind`` → factory map, loud on duplicate registration, with a clear error that
lists the known kinds". This is that mechanism, defined once, so a family is a
thin wrapper over an instance of it rather than a fresh copy of the same logic.

The unknown-kind error subclasses **both** ``KeyError`` and ``ValueError`` so a
family converging onto this registry keeps whichever exception contract its
callers already rely on (secrets raised ``KeyError``; directory raised
``ValueError``) — the convergence is behaviour-preserving.
"""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class UnknownConnectorKind(KeyError, ValueError):
    """Raised for an unregistered kind. Both a KeyError and a ValueError."""

    def __str__(self) -> str:  # KeyError.__str__ would add quotes; keep our text
        return self.args[0] if self.args else ""


class ConnectorRegistry(Generic[T]):
    """A process-local ``kind`` → factory registry.

    ``what`` names the family for error messages ("directory provider",
    "secret store", …). Registration is loud on a real duplicate (a different
    factory under a live key) but idempotent for the same factory, so
    import-time self-registration is safe under repeated imports.
    """

    def __init__(self, what: str) -> None:
        self._what = what
        self._reg: dict[str, T] = {}

    def register(self, kind: str, factory: T, *, replace: bool = False) -> None:
        if not kind:
            raise ValueError(f"{self._what}: kind must be non-empty")
        existing = self._reg.get(kind)
        if existing is not None and existing is not factory and not replace:
            raise ValueError(
                f"{self._what} kind {kind!r} already registered by "
                f"{getattr(existing, '__module__', '?')}."
                f"{getattr(existing, '__qualname__', existing)}; refusing to clobber"
            )
        self._reg[kind] = factory

    def unregister(self, kind: str) -> None:
        self._reg.pop(kind, None)

    def available(self) -> tuple[str, ...]:
        return tuple(sorted(self._reg))

    def values(self) -> list[T]:
        """Registered entries in registration order. For instance-registries
        (which store live objects, not factories) that iterate them — order is
        preserved because a family's dispatch may depend on it. (``available()``
        stays sorted; that is the names contract.)"""
        return list(self._reg.values())

    def items(self) -> list[tuple[str, T]]:
        return list(self._reg.items())

    def get(self, kind: str) -> T:
        try:
            return self._reg[kind]
        except KeyError:
            known = ", ".join(self.available()) or "(none registered)"
            raise UnknownConnectorKind(
                f"unknown {self._what} {kind!r}; known: {known}"
            ) from None

    def create(self, kind: str, *args, **kwargs):
        return self.get(kind)(*args, **kwargs)


__all__ = ["ConnectorRegistry", "UnknownConnectorKind"]
