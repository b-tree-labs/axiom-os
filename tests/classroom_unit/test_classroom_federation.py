# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for classroom ↔ federation primitive mapping.

Per spec-classroom.md §5.11 + ADR-022/023. A Classroom is an
ephemeral federation cohort:
- Coordinator = instructor's node
- Members = student nodes (one per student, in Topology A: student
  lives on the instructor's hub node; in Topology B+: student has
  their own node)

Mapping:
- enroll(student) → federation join(member_node)
- quarantine(student_node) → classroom access suspended
- revoke(student_node) → removed from cohort
- course pack distribution → A2A broadcast from coordinator
"""

from __future__ import annotations

import pytest


class TestCohortCreation:
    def test_create_cohort_with_coordinator(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            create_cohort,
        )

        cohort = create_cohort(
            classroom_id="cr",
            coordinator_node="example-host.example.org",
        )
        assert cohort.classroom_id == "cr"
        assert cohort.coordinator_node == "example-host.example.org"
        assert cohort.members == []
        assert cohort.created_at


class TestMemberJoin:
    def test_student_node_joins_cohort(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
        )

        cohort = create_cohort("cr", "example-host.example.org")
        cohort = add_member(
            cohort,
            student_id="s1",
            member_node="prague.axiom.eu",
            invite_token="tok-abc",
        )

        assert len(cohort.members) == 1
        m = cohort.members[0]
        assert m.student_id == "s1"
        assert m.member_node == "prague.axiom.eu"
        assert m.status == "ACTIVE"

    def test_reuse_student_id_updates_existing_member(self):
        """Same student_id re-joining should update, not duplicate."""
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "prague.axiom.eu", "t1")
        cohort = add_member(cohort, "s1", "prague.axiom.eu", "t2")
        assert len(cohort.members) == 1


class TestQuarantineCascade:
    """Student node quarantined by federation → classroom access
    suspended until recovery ceremony (§project_quarantine_and_recovery)."""

    def test_quarantine_suspends_access(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
            quarantine_member,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "prague.axiom.eu", "t")
        cohort = quarantine_member(cohort, "s1", reason="trust-chain break")

        m = cohort.members[0]
        assert m.status == "QUARANTINED"
        assert m.quarantine_reason == "trust-chain break"

    def test_can_check_access(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
            member_has_access,
            quarantine_member,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "prague.axiom.eu", "t")
        assert member_has_access(cohort, "s1") is True

        cohort = quarantine_member(cohort, "s1", reason="x")
        assert member_has_access(cohort, "s1") is False


class TestRecoveryCeremony:
    def test_recover_restores_access(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
            member_has_access,
            quarantine_member,
            recover_member,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "prague.axiom.eu", "t")
        cohort = quarantine_member(cohort, "s1", reason="x")
        cohort = recover_member(cohort, "s1", approver="ben@ut.edu")

        assert member_has_access(cohort, "s1") is True
        m = cohort.members[0]
        assert m.status == "ACTIVE"
        assert m.recovery_approver == "ben@ut.edu"


class TestRevoke:
    def test_revoke_terminal_removes_from_active(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
            member_has_access,
            revoke_member,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "prague.axiom.eu", "t")
        cohort = revoke_member(cohort, "s1", reason="withdrew")

        m = cohort.members[0]
        assert m.status == "REVOKED"
        assert member_has_access(cohort, "s1") is False

    def test_revoked_cannot_recover(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
            recover_member,
            revoke_member,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "prague.axiom.eu", "t")
        cohort = revoke_member(cohort, "s1", reason="x")

        with pytest.raises(ValueError, match="REVOKED"):
            recover_member(cohort, "s1", approver="i")


class TestBroadcast:
    """Coordinator broadcasts (e.g. updated course pack) to members."""

    def test_build_broadcast_to_all_active(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            broadcast_recipients,
            create_cohort,
            quarantine_member,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "n1", "t")
        cohort = add_member(cohort, "s2", "n2", "t")
        cohort = add_member(cohort, "s3", "n3", "t")
        cohort = quarantine_member(cohort, "s3", reason="x")

        recipients = broadcast_recipients(cohort)
        assert set(recipients) == {"n1", "n2"}
        # Quarantined n3 excluded from broadcasts


class TestMembershipClaim:
    def test_serialize_membership_proof(self):
        from axiom.extensions.builtins.classroom.classroom_federation import (
            add_member,
            create_cohort,
            serialize_membership_proof,
        )

        cohort = create_cohort("cr", "hub")
        cohort = add_member(cohort, "s1", "prague", "tok")

        proof = serialize_membership_proof(cohort, "s1")
        assert proof["student_id"] == "s1"
        assert proof["classroom_id"] == "cr"
        assert proof["coordinator_node"] == "hub"
        assert proof["member_node"] == "prague"
        assert proof["status"] == "ACTIVE"
        assert "signature" in proof
