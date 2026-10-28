# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for multi-node classroom topology (§2.10 Topology B).

A single classroom can span multiple institutional nodes — UT + OSU
+ INL teach a joint course, each with their own roster on their own
node. Shared course pack, federated metrics, peer-to-peer enrollment.

This is a stretch beyond Prague MVP (Topology A: hub-and-spoke) and
models the architecture so MVP code doesn't paint us into a
hub-centric corner.
"""

from __future__ import annotations

import pytest


class TestMultiNodeCohort:
    def test_create_multi_node_cohort(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            create_multi_node_cohort,
        )

        cohort = create_multi_node_cohort(
            classroom_id="joint-ne-2026",
            participating_nodes=[
                {"node": "ut.axiom.edu", "role": "lead", "institution": "UT"},
                {"node": "osu.axiom.edu", "role": "peer", "institution": "OSU"},
                {"node": "inl.axiom.edu", "role": "peer", "institution": "INL"},
            ],
        )
        assert cohort.classroom_id == "joint-ne-2026"
        assert len(cohort.nodes) == 3
        assert cohort.lead_node == "ut.axiom.edu"

    def test_exactly_one_lead_required(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            create_multi_node_cohort,
        )

        with pytest.raises(ValueError, match="lead"):
            create_multi_node_cohort(
                classroom_id="x",
                participating_nodes=[
                    {"node": "a", "role": "peer", "institution": "A"},
                    {"node": "b", "role": "peer", "institution": "B"},
                ],
            )


class TestPerNodeRosters:
    def test_students_enrolled_on_their_home_node(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            create_multi_node_cohort,
            enroll_student_on_node,
        )

        cohort = create_multi_node_cohort(
            classroom_id="j",
            participating_nodes=[
                {"node": "ut.axiom.edu", "role": "lead", "institution": "UT"},
                {"node": "osu.axiom.edu", "role": "peer", "institution": "OSU"},
            ],
        )
        cohort = enroll_student_on_node(cohort, node="ut.axiom.edu", student_id="s1")
        cohort = enroll_student_on_node(cohort, node="ut.axiom.edu", student_id="s2")
        cohort = enroll_student_on_node(cohort, node="osu.axiom.edu", student_id="s3")

        assert cohort.rosters["ut.axiom.edu"] == ["s1", "s2"]
        assert cohort.rosters["osu.axiom.edu"] == ["s3"]

    def test_enroll_on_unknown_node_raises(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            create_multi_node_cohort,
            enroll_student_on_node,
        )

        cohort = create_multi_node_cohort(
            classroom_id="j",
            participating_nodes=[
                {"node": "ut.axiom.edu", "role": "lead", "institution": "UT"},
            ],
        )
        with pytest.raises(ValueError, match="not in cohort"):
            enroll_student_on_node(cohort, node="unknown.axiom.edu", student_id="s1")


class TestSharedRAGManifest:
    def test_shared_corpus_plus_node_overlays(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            create_multi_node_cohort,
            set_node_overlay,
            set_shared_rag,
        )

        cohort = create_multi_node_cohort(
            classroom_id="j",
            participating_nodes=[
                {"node": "ut", "role": "lead", "institution": "UT"},
                {"node": "osu", "role": "peer", "institution": "OSU"},
            ],
        )
        cohort = set_shared_rag(
            cohort, pack_version="1.0.0", pack_path="s3://shared/pack-1.0.0.axiompack"
        )
        cohort = set_node_overlay(
            cohort, node="osu",
            overlay_path="s3://osu-local/osu-supplement.axiompack",
        )

        assert cohort.shared_rag["pack_version"] == "1.0.0"
        assert cohort.node_overlays["osu"] == (
            "s3://osu-local/osu-supplement.axiompack"
        )


class TestFederatedGradeAggregation:
    def test_merge_grade_claims_from_peers(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            aggregate_multi_node_grades,
        )

        # Each node contributes signed grade claims for its own students
        ut_claims = [
            {"student_id": "s1", "assessment_id": "pre", "question_id": "Q1",
             "final_score": 0.9, "signer_node": "ut",
             "signature": "sig-ut"},
        ]
        osu_claims = [
            {"student_id": "s3", "assessment_id": "pre", "question_id": "Q1",
             "final_score": 0.7, "signer_node": "osu",
             "signature": "sig-osu"},
        ]

        merged = aggregate_multi_node_grades(
            claims_by_node={"ut": ut_claims, "osu": osu_claims},
            trust_verifier=lambda c: True,
        )
        assert merged["total_students"] == 2
        assert merged["per_node_counts"] == {"ut": 1, "osu": 1}
        assert len(merged["grades"]) == 2

    def test_untrusted_node_claims_excluded(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            aggregate_multi_node_grades,
        )

        claims_by_node = {
            "ut": [{"student_id": "s1", "assessment_id": "x",
                    "question_id": "Q", "final_score": 0.9,
                    "signer_node": "ut", "signature": "valid"}],
            "attacker": [{"student_id": "fake", "assessment_id": "x",
                          "question_id": "Q", "final_score": 1.0,
                          "signer_node": "attacker", "signature": "bad"}],
        }
        merged = aggregate_multi_node_grades(
            claims_by_node=claims_by_node,
            trust_verifier=lambda c: c["signature"] == "valid",
        )
        assert merged["total_students"] == 1
        assert merged["rejected"] == 1


class TestPeerToPeerInvite:
    """Peer nodes can receive invite tokens from the lead node
    to enroll in the shared classroom."""

    def test_build_peer_invite_payload(self):
        from axiom.extensions.builtins.classroom.multi_node import (
            build_peer_invite,
            create_multi_node_cohort,
        )

        cohort = create_multi_node_cohort(
            classroom_id="j",
            participating_nodes=[
                {"node": "ut", "role": "lead", "institution": "UT"},
                {"node": "osu", "role": "peer", "institution": "OSU"},
            ],
        )
        invite = build_peer_invite(
            cohort, invitee_node="osu", invite_token="tok-xyz",
        )
        assert invite["classroom_id"] == "j"
        assert invite["lead_node"] == "ut"
        assert invite["invitee_node"] == "osu"
        assert invite["invite_token"] == "tok-xyz"
        assert "signature" in invite
