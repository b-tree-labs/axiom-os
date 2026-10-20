# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Membership → relationship tuples — ADR-103 decision 9.

A **reconcile, not an append**. The distinction is the whole point: an
append-only sync passes every test that adds a person and silently voids
ADR-086's guarantee that revoking a human starves every capability derived from
them. So the diff is computed against what the store actually holds, removals
are first-class, and an empty desired set removes everyone rather than being
mistaken for "nothing to do".

The store is an injected port — ``list_members`` + ``write`` — mirroring the way
``authz``'s :class:`FgaCheckClient` keeps the substrate testable without a
server. Nothing here talks to OpenFGA directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from axiom.extensions.builtins.directory.protocol import GroupRef
from axiom.extensions.builtins.directory.resolution import Membership
from axiom.extensions.builtins.directory.revoked import RevokedSet

#: Zanzibar convention: group:<id>#member@user:<subject>
USER_PREFIX = "user:"
GROUP_PREFIX = "group:"
MEMBER_RELATION = "member"


class StaleProjectionError(RuntimeError):
    """Refuses to reconcile from a degraded projection.

    Decisions 9 and 12 together: a directory outage degrades resolution to
    claims-only. Feeding that into a reconciler would delete every tuple the
    token happens not to mention, turning an outage into mass revocation — the
    most damaging thing this component could do.
    """


@dataclass(frozen=True)
class RelationTuple:
    user: str
    relation: str
    object: str


@runtime_checkable
class TupleStore(Protocol):
    def list_members(self, object: str, relation: str) -> Iterable[str]: ...

    def write(
        self,
        adds: Iterable[RelationTuple] = (),
        deletes: Iterable[RelationTuple] = (),
    ) -> None: ...


@dataclass(frozen=True)
class ReconcileResult:
    group_id: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


class MembershipReconciler:
    def __init__(
        self,
        *,
        store: TupleStore,
        revoked: RevokedSet | None = None,
        split_writes: bool = False,
    ) -> None:
        self._store = store
        self._revoked = revoked
        #: Emit deletes in their own write ahead of adds, so a truncated or
        #: partially-applied batch can only ever under-grant, never over-grant.
        self._split_writes = split_writes

    def reconcile(
        self,
        group: GroupRef,
        *,
        desired_subjects: Iterable[str],
        now: float | None = None,
    ) -> ReconcileResult:
        now = time.time() if now is None else now
        obj = f"{GROUP_PREFIX}{group.id}"
        desired = {str(s) for s in desired_subjects}
        current = {
            u[len(USER_PREFIX) :] if u.startswith(USER_PREFIX) else u
            for u in self._store.list_members(obj, MEMBER_RELATION)
        }

        add_subjects = sorted(desired - current)
        del_subjects = sorted(current - desired)

        if add_subjects or del_subjects:
            adds = tuple(
                RelationTuple(f"{USER_PREFIX}{s}", MEMBER_RELATION, obj) for s in add_subjects
            )
            deletes = tuple(
                RelationTuple(f"{USER_PREFIX}{s}", MEMBER_RELATION, obj) for s in del_subjects
            )
            if self._split_writes and deletes:
                self._store.write(deletes=deletes)
                if adds:
                    self._store.write(adds=adds)
            else:
                self._store.write(adds=adds, deletes=deletes)

        if self._revoked is not None:
            for s in del_subjects:
                self._revoked.record(s, group.id, now=now)
            for s in add_subjects:
                # A re-grant clears the fast-path block; otherwise someone
                # re-added stays locked out until the window elapses.
                self._revoked.clear_entry(s, group.id)

        return ReconcileResult(
            group_id=group.id,
            added=tuple(add_subjects),
            removed=tuple(del_subjects),
            unchanged=tuple(sorted(desired & current)),
        )

    def reconcile_membership(
        self, membership: Membership, *, now: float | None = None
    ) -> tuple[ReconcileResult, ...]:
        """Project one principal's membership. Refuses a stale projection."""
        if membership.stale:
            raise StaleProjectionError(
                f"projection for {membership.principal.subject} is stale "
                f"(source={membership.source}); refusing to drive deletions from it"
            )
        return tuple(
            self.reconcile(g, desired_subjects=[membership.principal.subject], now=now)
            for g in membership.groups
        )


__all__ = [
    "MEMBER_RELATION",
    "MembershipReconciler",
    "ReconcileResult",
    "RelationTuple",
    "StaleProjectionError",
    "TupleStore",
]
