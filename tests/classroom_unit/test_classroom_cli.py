# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for `axi classroom` CLI — the orchestration layer.

The CLI wires all 10 classroom modules together:
  axi classroom create → Canvas + enrollment + rails + RAG config
  axi classroom status → health dashboard
  axi classroom doctor → health checks (web, tokens, trace, RAG, LLM)
  axi classroom enroll → add/sync students
  axi classroom rag-policy → view/swap RAG policy
  axi classroom ingest → media ingest shortcut
"""

from __future__ import annotations


class TestClassroomCreate:
    def test_create_from_manifest_and_canvas(self):
        from axiom.extensions.builtins.classroom.classroom_cli import create_classroom
        from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

        mock = CanvasMockServer()
        mock.add_course("101", "STEM 2026")
        mock.add_enrollment(
            "101",
            {"user_id": "s1", "name": "Alice", "email": "a@ut.edu", "type": "StudentEnrollment"},
        )
        mock.add_enrollment(
            "101",
            {"user_id": "s2", "name": "Bob", "email": "b@ut.edu", "type": "StudentEnrollment"},
        )

        manifest = {
            "id": "ne-prague-2026",
            "title": "NE Prague 2026",
            "version": "1.0.0",
            "system_prompt": "You are a helpful tutor.",
            "onboarding_rails": [
                {
                    "id": "interview",
                    "source": "custom",
                    "required": True,
                    "questions": [{"id": "Q1", "text": "Hello?", "type": "free_text"}],
                },
            ],
        }

        result = create_classroom(
            manifest=manifest,
            lms_config={"api_url": mock.url, "api_token": "t", "_mock_server": mock},
            canvas_course_id="101",
            instructor_email="ben@ut.edu",
            nationality_map={},
            rag_config={"mode": "course_only"},
            ttl_days=30,
        )

        assert result.classroom_id == "ne-prague-2026"
        assert result.student_count == 2
        assert result.pipeline is not None  # ClassroomChatPipeline configured
        assert result.rag_policy is not None  # RAGPolicy configured
        assert len(result.enrollment.tokens) == 2


class TestClassroomDoctor:
    def test_doctor_returns_health_checks(self):
        from unittest.mock import patch

        from axiom.extensions.builtins.classroom.classroom_cli import classroom_doctor

        # Mock the web-endpoint probe: the check makes a real HTTP GET to the
        # provided URL, which is non-deterministic in CI (nothing on :8080).
        # The intent of this test is to assert all-healthy when signals are
        # green, not to hit the network.
        with patch(
            "axiom.extensions.builtins.classroom.classroom_cli._check_web_endpoint",
            return_value=True,
        ):
            checks = classroom_doctor(
                classroom_id="test",
                web_endpoint="http://localhost:8080",
                trace_store_writable=True,
                rag_indexed=True,
                llm_responsive=True,
                tokens_valid=True,
            )

        assert len(checks) >= 5
        assert all(hasattr(c, "name") and hasattr(c, "status") for c in checks)
        # All should pass with good inputs
        assert all(c.status == "healthy" for c in checks)

    def test_doctor_detects_unhealthy(self):
        from axiom.extensions.builtins.classroom.classroom_cli import classroom_doctor

        checks = classroom_doctor(
            classroom_id="test",
            web_endpoint="http://unreachable:9999",
            trace_store_writable=False,
            rag_indexed=True,
            llm_responsive=False,
            tokens_valid=True,
        )

        unhealthy = [c for c in checks if c.status != "healthy"]
        assert len(unhealthy) >= 2  # trace store + LLM at minimum


class TestClassroomRAGPolicySwap:
    def test_swap_rag_policy(self):
        from axiom.extensions.builtins.classroom.classroom_cli import (
            ClassroomInstance,
            swap_rag_policy,
        )
        from axiom.extensions.builtins.classroom.rag_policy import RAGPolicy

        p1 = RAGPolicy(id="course-only", name="Course", corpora=[{"corpus_id": "c"}])
        p2 = RAGPolicy(
            id="full", name="Full", corpora=[{"corpus_id": "c"}, {"corpus_id": "community"}]
        )

        instance = ClassroomInstance(classroom_id="test", rag_policy=p1)
        swap_rag_policy(instance, p2)

        assert instance.rag_policy.id == "full"
        assert len(instance.rag_policy.corpora) == 2
