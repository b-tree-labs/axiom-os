# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for ownership model (#46).

Per ADR-026 (draft). Ownership is distinct from access and scope:
- Access: can this principal see it right now? (bipartite graphs)
- Scope: where does it logically live? (cognitive type + retention tier)
- Ownership: who is the authoritative controller?

Model:
- Single master + peer delegations (no co-ownership)
- Delegations time-bounded, revocable
- Four independent rights: CONTROL, GOALS, RESOURCES, EFFORT
- Trust target decomposition: (principal, role, context)
- Transfer = full transfer (not re-delegation)
"""

from __future__ import annotations

import pytest


class TestRights:
    def test_four_rights_defined(self):
        from axiom.memory.ownership import Right

        assert Right.CONTROL.value == "control"
        assert Right.GOALS.value == "goals"
        assert Right.RESOURCES.value == "resources"
        assert Right.EFFORT.value == "effort"

    def test_all_rights_helper(self):
        from axiom.memory.ownership import Right, all_rights

        assert all_rights() == frozenset(Right)


class TestOwnershipConstruction:
    def test_new_ownership_has_master_no_delegations(self):
        from axiom.memory.ownership import new_ownership

        o = new_ownership(master="@ben:ut")
        assert o.master == "@ben:ut"
        assert o.delegations == ()

    def test_new_ownership_is_frozen(self):
        from axiom.memory.ownership import new_ownership

        o = new_ownership(master="@ben:ut")
        with pytest.raises(Exception):  # noqa: B017  # FrozenInstanceError
            o.master = "@alice:ut"


class TestDelegation:
    def test_delegate_with_partial_rights(self):
        from axiom.memory.ownership import Right, delegate, new_ownership

        o = new_ownership(master="@ben:ut")
        o = delegate(
            o,
            delegate_principal="axi",
            rights={Right.CONTROL, Right.EFFORT},
            expires_at="2026-12-31T23:59:59Z",
        )
        assert len(o.delegations) == 1
        d = o.delegations[0]
        assert d.delegate == "axi"
        assert Right.CONTROL in d.rights
        assert Right.EFFORT in d.rights
        assert Right.GOALS not in d.rights

    def test_delegate_rejects_unknown_rights(self):
        from axiom.memory.ownership import delegate, new_ownership

        o = new_ownership(master="@ben:ut")
        with pytest.raises(ValueError, match="unknown right"):
            delegate(
                o,
                delegate_principal="axi",
                rights={"made-up-right"},  # type: ignore[arg-type]
                expires_at="2026-12-31T23:59:59Z",
            )


class TestCanExercise:
    def test_master_can_exercise_every_right(self):
        from axiom.memory.ownership import Right, can_exercise, new_ownership

        o = new_ownership(master="@ben:ut")
        for r in Right:
            assert can_exercise(o, principal="@ben:ut", right=r,
                                at="2026-06-01T00:00:00Z") is True

    def test_delegate_can_only_exercise_granted_rights(self):
        from axiom.memory.ownership import Right, can_exercise, delegate, new_ownership

        o = new_ownership(master="@ben:ut")
        o = delegate(o, "axi", {Right.EFFORT}, "2026-12-31T23:59:59Z")

        assert can_exercise(o, "axi", Right.EFFORT, "2026-06-01T00:00:00Z") is True
        assert can_exercise(o, "axi", Right.CONTROL, "2026-06-01T00:00:00Z") is False

    def test_expired_delegation_cannot_exercise(self):
        from axiom.memory.ownership import Right, can_exercise, delegate, new_ownership

        o = new_ownership(master="@ben:ut")
        o = delegate(o, "axi", {Right.CONTROL}, "2025-12-31T23:59:59Z")
        # Past the expiry
        assert can_exercise(o, "axi", Right.CONTROL, "2026-06-01T00:00:00Z") is False

    def test_unknown_principal_cannot_exercise(self):
        from axiom.memory.ownership import Right, can_exercise, new_ownership

        o = new_ownership(master="@ben:ut")
        assert can_exercise(o, "@stranger:xx", Right.CONTROL,
                             "2026-06-01T00:00:00Z") is False


class TestRevoke:
    def test_revoke_removes_delegation(self):
        from axiom.memory.ownership import (
            Right,
            can_exercise,
            delegate,
            new_ownership,
            revoke_delegation,
        )

        o = new_ownership(master="@ben:ut")
        o = delegate(o, "axi", {Right.CONTROL}, "2026-12-31T23:59:59Z")
        assert can_exercise(o, "axi", Right.CONTROL, "2026-06-01T00:00:00Z")

        o = revoke_delegation(o, delegate_principal="axi")
        assert can_exercise(o, "axi", Right.CONTROL, "2026-06-01T00:00:00Z") is False


class TestTransfer:
    def test_transfer_changes_master(self):
        from axiom.memory.ownership import new_ownership, transfer

        o = new_ownership(master="@ben:ut")
        o = transfer(
            o,
            new_master="@alice:ut",
            outgoing_signature=b"ben-signed-transfer",
            incoming_acceptance=b"alice-accepted",
        )
        assert o.master == "@alice:ut"

    def test_transfer_requires_both_signatures(self):
        from axiom.memory.ownership import new_ownership, transfer

        o = new_ownership(master="@ben:ut")
        with pytest.raises(ValueError, match="signature"):
            transfer(
                o, new_master="@alice:ut",
                outgoing_signature=None,
                incoming_acceptance=b"alice-accepted",
            )
        with pytest.raises(ValueError, match="acceptance"):
            transfer(
                o, new_master="@alice:ut",
                outgoing_signature=b"ben-signed",
                incoming_acceptance=None,
            )

    def test_transfer_clears_old_delegations(self):
        """Transfers are clean breaks — old delegations don't carry over."""
        from axiom.memory.ownership import Right, delegate, new_ownership, transfer

        o = new_ownership(master="@ben:ut")
        o = delegate(o, "axi", {Right.CONTROL}, "2026-12-31T23:59:59Z")
        o = transfer(o, "@alice:ut", b"sig-out", b"sig-in")
        assert o.delegations == ()
        assert o.master == "@alice:ut"


class TestTrustTarget:
    def test_principal_only(self):
        from axiom.memory.ownership import TrustTarget

        t = TrustTarget(principal="@ben:ut", role=None, context="general")
        assert t.is_human_scoped is True
        assert t.is_role_scoped is False

    def test_role_only(self):
        from axiom.memory.ownership import TrustTarget

        t = TrustTarget(principal=None, role="@ut-nuclear-faculty",
                        context="reactor-physics")
        assert t.is_human_scoped is False
        assert t.is_role_scoped is True

    def test_both_human_and_role(self):
        from axiom.memory.ownership import TrustTarget

        t = TrustTarget(principal="@ben:ut", role="@ut-nuclear-faculty",
                        context="teaching")
        assert t.is_human_scoped is True
        assert t.is_role_scoped is True


class TestOwnershipFragmentIntegration:
    """Ownership can be attached to a MemoryFragment via a dedicated slot."""

    def test_fragment_carries_ownership(self):
        import dataclasses

        from axiom.memory.fragment import create_fragment
        from axiom.memory.ownership import new_ownership

        f = create_fragment(
            content={"fact": "x"}, cognitive_type="semantic",
            principal_id="@ben:ut", agents=set(), resources=set(),
        )
        owned = dataclasses.replace(f, ownership=new_ownership(master="@ben:ut"))
        assert owned.ownership is not None
        assert owned.ownership.master == "@ben:ut"


class TestSuccessionCeremony:
    """Role transitions rebind role-scoped trust to the new occupant.

    Human-scoped trust stays with the departing human.
    """

    def test_role_succession_requires_outgoing_consent(self):
        from axiom.memory.ownership import role_succession

        from_signature = b"alice-signed-stepping-down"
        to_signature = b"bob-accepts"
        record = role_succession(
            role="@ut-nuclear-faculty-chair",
            outgoing_principal="@alice:ut",
            incoming_principal="@bob:ut",
            outgoing_signature=from_signature,
            incoming_signature=to_signature,
            effective_at="2026-06-01T00:00:00Z",
        )
        assert record["role"] == "@ut-nuclear-faculty-chair"
        assert record["from"] == "@alice:ut"
        assert record["to"] == "@bob:ut"
        assert record["effective_at"] == "2026-06-01T00:00:00Z"
        assert record["outgoing_signature"] == from_signature

    def test_role_succession_missing_signature_refuses(self):
        from axiom.memory.ownership import role_succession

        with pytest.raises(ValueError, match="signature"):
            role_succession(
                role="@chair",
                outgoing_principal="@alice:ut",
                incoming_principal="@bob:ut",
                outgoing_signature=None,
                incoming_signature=b"bob-accepts",
                effective_at="2026-06-01T00:00:00Z",
            )
