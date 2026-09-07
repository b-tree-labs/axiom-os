# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-103 phase 2 — the reconciler and the revoked set.

Decision 9 (reconcile, not append) and decision 10 (revocations propagate ahead
of grants). Both exist to protect ADR-086's guarantee that revoking a human
starves every capability derived from them, so the removal paths are the tests
that matter here — an append-only sync would pass every "add a person" test
while silently voiding it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.directory import (
    GroupRef,
    GroupRoleMap,
    MembershipResolver,
    PrincipalRef,
)
from axiom.extensions.builtins.directory.providers import LocalDirectory
from axiom.extensions.builtins.directory.reconcile import (
    MembershipReconciler,
    RelationTuple,
    StaleProjectionError,
)
from axiom.extensions.builtins.directory.revoked import RevokedSet

OPS = GroupRef(id="g-ops", provider="local")


class FakeTupleStore:
    """In-memory stand-in for the FGA store, recording every write."""

    def __init__(self, initial: dict[str, set[str]] | None = None) -> None:
        self._members: dict[str, set[str]] = {
            k: set(v) for k, v in (initial or {}).items()
        }
        self.writes: list[tuple[tuple, tuple]] = []

    def list_members(self, object: str, relation: str) -> list[str]:
        return sorted(self._members.get(object, set()))

    def write(self, adds=(), deletes=()) -> None:
        self.writes.append((tuple(adds), tuple(deletes)))
        for t in deletes:
            self._members.get(t.object, set()).discard(t.user)
        for t in adds:
            self._members.setdefault(t.object, set()).add(t.user)


# --------------------------------------------------------------------------
# Decision 9 — reconcile, not append. Removals are the point.
# --------------------------------------------------------------------------


class TestReconcile:
    def test_adds_missing_members(self):
        store = FakeTupleStore()
        r = MembershipReconciler(store=store)
        result = r.reconcile(OPS, desired_subjects=["oid-1", "oid-2"])
        assert set(result.added) == {"oid-1", "oid-2"}
        assert result.removed == ()
        assert store.list_members("group:g-ops", "member") == ["user:oid-1", "user:oid-2"]

    def test_removes_members_no_longer_present(self):
        """THE test. An append-only sync passes everything above and fails here."""
        store = FakeTupleStore({"group:g-ops": {"user:oid-1", "user:oid-gone"}})
        r = MembershipReconciler(store=store)
        result = r.reconcile(OPS, desired_subjects=["oid-1"])
        assert result.removed == ("oid-gone",)
        assert store.list_members("group:g-ops", "member") == ["user:oid-1"]

    def test_is_idempotent(self):
        store = FakeTupleStore()
        r = MembershipReconciler(store=store)
        r.reconcile(OPS, desired_subjects=["oid-1"])
        before = len(store.writes)
        second = r.reconcile(OPS, desired_subjects=["oid-1"])
        assert second.added == () and second.removed == ()
        assert len(store.writes) == before, "a no-op must not write"

    def test_an_empty_desired_set_removes_everyone(self):
        """'Empty means skip' is the bug that makes offboarding silently fail."""
        store = FakeTupleStore({"group:g-ops": {"user:oid-1", "user:oid-2"}})
        result = MembershipReconciler(store=store).reconcile(OPS, desired_subjects=[])
        assert set(result.removed) == {"oid-1", "oid-2"}
        assert store.list_members("group:g-ops", "member") == []

    def test_tuple_shape_is_the_zanzibar_convention(self):
        store = FakeTupleStore()
        MembershipReconciler(store=store).reconcile(OPS, desired_subjects=["oid-1"])
        adds, _ = store.writes[0]
        assert adds[0] == RelationTuple(
            user="user:oid-1", relation="member", object="group:g-ops"
        )

    def test_deletes_are_written_before_adds(self):
        """Revocation must not wait behind a grant if the write is truncated."""
        calls = []

        class OrderingStore(FakeTupleStore):
            def write(self, adds=(), deletes=()):
                calls.append(("deletes" if deletes else "adds"))
                super().write(adds, deletes)

        ordering = OrderingStore({"group:g-ops": {"user:oid-gone"}})
        MembershipReconciler(store=ordering, split_writes=True).reconcile(
            OPS, desired_subjects=["oid-new"]
        )
        assert calls[0] == "deletes"


# --------------------------------------------------------------------------
# Decision 12 x 9 — a degraded projection must never drive deletions
# --------------------------------------------------------------------------


class TestStaleProjectionRefusal:
    def test_reconciling_from_a_stale_membership_is_refused(self):
        """Combining decisions 9 and 12: a directory outage degrades resolution
        to claims-only. Feeding that into a reconciler would delete every tuple
        the token happens not to mention — turning an outage into mass
        revocation."""

        class Boom(LocalDirectory):
            def groups_for(self, principal):
                raise ConnectionError("directory unreachable")

        resolver = MembershipResolver(
            provider=Boom(groups={}), role_map=GroupRoleMap.permissive()
        )
        membership = resolver.resolve(PrincipalRef("oid-1", "local"), claims={}, now=0.0)
        assert membership.stale is True

        store = FakeTupleStore({"group:g-ops": {"user:oid-1"}})
        with pytest.raises(StaleProjectionError):
            MembershipReconciler(store=store).reconcile_membership(membership)
        assert store.writes == [], "an outage must not delete anything"


# --------------------------------------------------------------------------
# Decision 10 — revocations propagate ahead of grants
# --------------------------------------------------------------------------


class TestRevokedSet:
    def test_reconciler_records_removals(self):
        store = FakeTupleStore({"group:g-ops": {"user:oid-gone"}})
        revoked = RevokedSet()
        MembershipReconciler(store=store, revoked=revoked).reconcile(
            OPS, desired_subjects=[], now=100.0
        )
        assert revoked.is_revoked("oid-gone", "g-ops", now=100.0) is True

    def test_resolver_drops_a_revoked_group_even_when_claims_still_assert_it(self):
        """The crown jewel of decision 10.

        A token minted before the revocation still carries the group. Without
        this, the person keeps their access until the token refreshes — exactly
        the window ADR-086 exists to close.
        """
        revoked = RevokedSet()
        revoked.record("oid-1", "g-ops", now=0.0)
        resolver = MembershipResolver(
            provider=LocalDirectory(groups={}),
            role_map=GroupRoleMap.permissive(),
            revoked=revoked,
        )
        m = resolver.resolve(
            PrincipalRef("oid-1", "local"),
            claims={"groups": ["g-ops", "g-eng"]},
            now=1.0,
        )
        assert [g.id for g in m.groups] == ["g-eng"]
        assert "g-ops" not in m.roles

    def test_revocation_expires_so_the_set_stays_bounded(self):
        revoked = RevokedSet(window=10.0)
        revoked.record("oid-1", "g-ops", now=0.0)
        assert revoked.is_revoked("oid-1", "g-ops", now=5.0) is True
        assert revoked.is_revoked("oid-1", "g-ops", now=999.0) is False

    def test_set_is_capacity_bounded(self):
        revoked = RevokedSet(max_entries=2)
        for i in range(5):
            revoked.record(f"oid-{i}", "g-ops", now=float(i))
        assert len(revoked) <= 2
        assert revoked.is_revoked("oid-4", "g-ops", now=4.0) is True

    def test_a_re_grant_clears_the_revocation(self):
        """Re-adding someone must not leave them locked out by a stale entry."""
        store = FakeTupleStore({"group:g-ops": {"user:oid-1"}})
        revoked = RevokedSet()
        rec = MembershipReconciler(store=store, revoked=revoked)
        rec.reconcile(OPS, desired_subjects=[], now=0.0)
        assert revoked.is_revoked("oid-1", "g-ops", now=1.0) is True
        rec.reconcile(OPS, desired_subjects=["oid-1"], now=2.0)
        assert revoked.is_revoked("oid-1", "g-ops", now=3.0) is False
