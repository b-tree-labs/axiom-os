# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The directory port — ADR-103 decision 1.

Deliberately small, with *declared capabilities* rather than a fat interface
every adapter has to fake. A provider that cannot enumerate members is a
first-class citizen: callers negotiate on :class:`DirectoryCapability` instead
of assuming, the same pattern the calendar vendor factory already proves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Protocol, runtime_checkable


class DirectoryCapability(str, Enum):
    """What an adapter can actually do.

    LOOKUP is the floor — resolve one principal's groups. Everything else is
    optional, because real directories differ: a token's claims can be read but
    never enumerated; a SCIM feed pushes rather than answers queries.
    """

    LOOKUP = "lookup"
    ENUMERATE = "enumerate"
    DELTA = "delta"
    PUSH = "push"


@dataclass(frozen=True)
class PrincipalRef:
    """A person, keyed on the IdP's **immutable subject identifier**.

    ADR-103 decision 11: ``display`` is cached for humans and is explicitly NOT
    part of identity — a name change must not orphan an account or invalidate a
    cache entry, so it is excluded from equality and hashing.
    """

    subject: str
    provider: str
    display: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class GroupRef:
    id: str
    provider: str
    display: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Group:
    ref: GroupRef
    display_name: str | None = None


@dataclass(frozen=True)
class DeltaPage:
    """One page of a delta walk. ``cursor`` is opaque and provider-specific."""

    changes: tuple[Any, ...] = ()
    cursor: str | None = None


@runtime_checkable
class DirectoryProvider(Protocol):
    """Resolve group membership from some authority.

    Only :meth:`groups_for` and :attr:`capabilities` are required. Optional
    methods raise :class:`NotImplementedError` rather than being absent, so a
    caller that ignores the capability set gets a clear refusal instead of an
    ``AttributeError`` three frames away.
    """

    capabilities: frozenset[DirectoryCapability]

    def groups_for(self, principal: PrincipalRef) -> list[GroupRef]: ...

    def resolve_group(self, ref: GroupRef) -> Group | None: ...

    def members_of(self, ref: GroupRef) -> Iterator[PrincipalRef]: ...

    def delta(self, since: str | None = None) -> DeltaPage: ...


__all__ = [
    "DeltaPage",
    "DirectoryCapability",
    "DirectoryProvider",
    "Group",
    "GroupRef",
    "PrincipalRef",
]
