# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for student readiness gate (WF-2).

Per spec-classroom.md §3.1 WF-2 (Onboarding tracking):
  "Manages per-student onboarding checklists (syllabus read,
   interview completed, pre-course work done, consent
   acknowledged). Reports readiness status to instructor.
   Enforces completion of required items before marking a
   student ready."

This gate blocks assessments/chat until the student is ready,
and blocks EC-tagged content until nationality attestation.
"""

from __future__ import annotations

import pytest


def _make_rail(rail_id: str, required: bool = True) -> dict:
    return {
        "id": rail_id,
        "source": "custom",
        "required": required,
        "questions": [
            {"id": "Q1", "text": "First question?", "type": "free_text"},
        ],
    }


class TestCreateReadiness:
    def test_create_includes_rails(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            create_student_readiness,
        )

        r = create_student_readiness(
            student_id="s1",
            classroom_id="ne-prague-2026",
            rails=[_make_rail("interview"), _make_rail("pre-quiz", required=False)],
            ec_required=False,
        )

        assert r.student_id == "s1"
        assert r.classroom_id == "ne-prague-2026"
        assert len(r.rails) == 2
        assert all(rc.status == "pending" for rc in r.rails)
        assert r.ec_attestation_required is False
        assert r.syllabus_acknowledged is False

    def test_create_with_ec_requires_attestation(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            create_student_readiness,
        )

        r = create_student_readiness(
            student_id="s1", classroom_id="c", rails=[], ec_required=True
        )
        assert r.ec_attestation_required is True
        assert r.ec_attestation_signed is False


class TestMarkers:
    def test_acknowledge_syllabus(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            acknowledge_syllabus,
            create_student_readiness,
        )

        r = create_student_readiness("s1", "c", rails=[])
        r = acknowledge_syllabus(r)
        assert r.syllabus_acknowledged is True

    def test_consent_given(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            create_student_readiness,
            give_consent,
        )

        r = create_student_readiness("s1", "c", rails=[])
        r = give_consent(r, "gdpr-v1")
        assert r.consent_given is True
        assert r.consent_version == "gdpr-v1"

    def test_complete_first_chat(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            complete_first_chat,
            create_student_readiness,
        )

        r = create_student_readiness("s1", "c", rails=[])
        r = complete_first_chat(r)
        assert r.first_chat_completed is True

    def test_sign_ec_attestation(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            create_student_readiness,
            sign_ec_attestation,
        )

        r = create_student_readiness("s1", "c", rails=[], ec_required=True)
        r = sign_ec_attestation(r, nationality="US", signed_by="s1")
        assert r.ec_attestation_signed is True
        assert r.ec_nationality == "US"


class TestRailProgress:
    def test_record_response_advances_rail(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            create_student_readiness,
            record_rail_response,
        )

        r = create_student_readiness(
            "s1", "c", rails=[_make_rail("interview")]
        )
        r = record_rail_response(r, rail_id="interview", question_id="Q1",
                                 answer="My background is physics.")

        rail = r.rails[0]
        assert rail.status == "in_progress"
        assert rail.responses["Q1"] == "My background is physics."

    def test_complete_rail_marks_done(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            complete_rail,
            create_student_readiness,
        )

        r = create_student_readiness(
            "s1", "c", rails=[_make_rail("interview")]
        )
        r = complete_rail(r, "interview")
        assert r.rails[0].status == "completed"

    def test_skip_non_required_rail(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            create_student_readiness,
            skip_rail,
        )

        r = create_student_readiness(
            "s1", "c", rails=[_make_rail("extra", required=False)]
        )
        r = skip_rail(r, "extra")
        assert r.rails[0].status == "skipped"

    def test_cannot_skip_required_rail(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            create_student_readiness,
            skip_rail,
        )

        r = create_student_readiness("s1", "c", rails=[_make_rail("must-do", required=True)])
        with pytest.raises(ValueError, match="required"):
            skip_rail(r, "must-do")


class TestIsReady:
    def test_ready_when_all_gates_met(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            acknowledge_syllabus,
            complete_first_chat,
            complete_rail,
            create_student_readiness,
            give_consent,
            is_student_ready,
        )

        r = create_student_readiness(
            "s1", "c", rails=[_make_rail("interview")]
        )
        r = acknowledge_syllabus(r)
        r = give_consent(r, "v1")
        r = complete_first_chat(r)
        r = complete_rail(r, "interview")

        ready, blockers = is_student_ready(r)
        assert ready is True
        assert blockers == []

    def test_missing_syllabus_blocks(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            complete_first_chat,
            create_student_readiness,
            give_consent,
            is_student_ready,
        )

        r = create_student_readiness("s1", "c", rails=[])
        r = give_consent(r, "v1")
        r = complete_first_chat(r)

        ready, blockers = is_student_ready(r)
        assert ready is False
        assert any("syllabus" in b.lower() for b in blockers)

    def test_missing_required_rail_blocks(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            acknowledge_syllabus,
            complete_first_chat,
            create_student_readiness,
            give_consent,
            is_student_ready,
        )

        r = create_student_readiness("s1", "c", rails=[_make_rail("interview", required=True)])
        r = acknowledge_syllabus(r)
        r = give_consent(r, "v1")
        r = complete_first_chat(r)

        ready, blockers = is_student_ready(r)
        assert ready is False
        assert any("interview" in b.lower() or "rail" in b.lower() for b in blockers)

    def test_ec_required_but_unsigned_blocks(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            acknowledge_syllabus,
            complete_first_chat,
            create_student_readiness,
            give_consent,
            is_student_ready,
        )

        r = create_student_readiness("s1", "c", rails=[], ec_required=True)
        r = acknowledge_syllabus(r)
        r = give_consent(r, "v1")
        r = complete_first_chat(r)

        ready, blockers = is_student_ready(r)
        assert ready is False
        assert any("attest" in b.lower() or "ec" in b.lower() for b in blockers)


class TestCohortReport:
    def test_cohort_report_shows_per_student_and_aggregate(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            acknowledge_syllabus,
            cohort_readiness_report,
            complete_first_chat,
            create_student_readiness,
            give_consent,
        )

        r1 = create_student_readiness("s1", "c", rails=[])
        r1 = acknowledge_syllabus(r1)
        r1 = give_consent(r1, "v1")
        r1 = complete_first_chat(r1)

        r2 = create_student_readiness("s2", "c", rails=[])  # not ready

        report = cohort_readiness_report([r1, r2])

        assert report["total"] == 2
        assert report["ready"] == 1
        assert report["not_ready"] == 1
        assert "s1" in [s["student_id"] for s in report["students"]]
        s2_row = next(s for s in report["students"] if s["student_id"] == "s2")
        assert s2_row["ready"] is False
        assert len(s2_row["blockers"]) > 0


class TestFederationSync:
    """Stretch: federation-aware readiness tracking (§5.11 + ADR-023).

    When student lives on a member node and instructor on hub node,
    readiness is queryable via a signed claim that rides over the
    A2A protocol. For the MVP we test the serialization surface —
    actual transport is wired in the federation task.
    """

    def test_serialize_for_federation_includes_signature_slot(self):
        from axiom.extensions.builtins.classroom.student_readiness import (
            acknowledge_syllabus,
            create_student_readiness,
            serialize_readiness_claim,
        )

        r = create_student_readiness("s1", "c", rails=[])
        r = acknowledge_syllabus(r)

        claim = serialize_readiness_claim(r, signer_node="prague.axiom.eu")
        assert claim["student_id"] == "s1"
        assert claim["classroom_id"] == "c"
        assert claim["signer_node"] == "prague.axiom.eu"
        assert "ready" in claim
        assert "blockers" in claim
        assert claim["syllabus_acknowledged"] is True
