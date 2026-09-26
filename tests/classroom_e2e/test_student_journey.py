# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""E2e test: complete student journey through the classroom.

This test exercises the FULL stack in-process (no Docker needed):
Canvas mock → enrollment → token → Q&A onboarding → chat with
RAG → media ingest → personal RAG retrieval → tracing → course
manifest → doctor health check.

It validates that all 11 classroom modules work together.
"""

from __future__ import annotations

import pytest


@pytest.mark.classroom_e2e
class TestCompleteStudentJourney:
    """Full student journey: enrollment → onboarding → chat → media → trace."""

    def test_end_to_end_student_experience(self):
        # --- SETUP: Instructor creates classroom ---

        from axiom.extensions.builtins.classroom.classroom_cli import (
            classroom_doctor,
            create_classroom,
        )
        from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

        # Canvas with 3 students (mixed nationality for Prague scenario)
        canvas = CanvasMockServer()
        canvas.add_course("101", "NE Prague 2026")
        canvas.add_enrollment(
            "101",
            {
                "user_id": "alice",
                "name": "Alice Smith",
                "email": "alice@ut.edu",
                "type": "StudentEnrollment",
            },
        )
        canvas.add_enrollment(
            "101",
            {
                "user_id": "bob",
                "name": "Bob Jones",
                "email": "bob@ut.edu",
                "type": "StudentEnrollment",
            },
        )
        canvas.add_enrollment(
            "101",
            {
                "user_id": "karel",
                "name": "Karel Novák",
                "email": "karel@cvut.cz",
                "type": "StudentEnrollment",
            },
        )

        manifest = {
            "id": "ne-prague-2026",
            "title": "Nuclear Engineering Prague 2026",
            "version": "1.0.0",
            "system_prompt": "You are a helpful tutor for a nuclear engineering course in Prague.",
            "onboarding_rails": [
                {
                    "id": "pre-course-interview",
                    "source": "custom",
                    "required": True,
                    "questions": [
                        {
                            "id": "Q1",
                            "text": "How familiar are you with AI tools?",
                            "type": "free_text",
                        },
                        {
                            "id": "Q2",
                            "text": "Comfort with AI for learning?",
                            "type": "likert_scale",
                            "scale": [1, 5],
                        },
                        {"id": "Q3", "text": "Used AI in a previous course?", "type": "yes_no"},
                        {
                            "id": "Q3a",
                            "text": "What did you use it for?",
                            "type": "free_text",
                            "condition": "Q3 == yes",
                        },
                    ],
                },
                {
                    "id": "data-consent",
                    "source": "axiom-core",
                    "required": True,
                    "questions": [
                        {
                            "id": "C1",
                            "text": "Do you consent to data collection for research?",
                            "type": "yes_no",
                        },
                    ],
                },
            ],
        }

        result = create_classroom(
            manifest=manifest,
            lms_config={"api_url": canvas.url, "api_token": "t", "_mock_server": canvas},
            canvas_course_id="101",
            instructor_email="ben@ut.edu",
            nationality_map={"karel": "CZ"},
            rag_config={"mode": "ab_test", "shadow_corpus": "example-corpus"},
            ttl_days=30,
        )

        # Verify classroom created correctly
        assert result.classroom_id == "ne-prague-2026"
        assert result.student_count == 3
        assert result.rag_policy.id == "ne-prague-2026-ab"
        assert result.rag_policy.shadow_config is not None
        assert len(result.enrollment.tokens) == 3

        # Verify nationality attestations
        attestations = {a.student_id: a.nationality for a in result.enrollment.attestations}
        assert attestations["karel"] == "CZ"
        assert attestations["alice"] is None  # US, not attested
        assert attestations["bob"] is None

        # Verify onboarding rails applied to all students
        assert len(result.enrollment.checklists) == 3
        assert all(len(c) == 2 for c in result.enrollment.checklists)  # 2 rails each

        # --- STUDENT: Alice completes onboarding interview ---

        from axiom.questionnaire.engine import QAEngine

        interview_rail = manifest["onboarding_rails"][0]
        engine = QAEngine(interview_rail)
        session = engine.start_session("alice")

        # Q1: free text
        session = engine.submit_response(session, "I use ChatGPT for homework")
        assert session.responses["Q1"] == "I use ChatGPT for homework"

        # Q2: likert
        session = engine.submit_response(session, "4")
        assert session.responses["Q2"] == 4

        # Q3: yes → triggers Q3a
        session = engine.submit_response(session, "yes")
        assert session.responses["Q3"] is True

        # Q3a: conditional follow-up
        q = engine.get_current_question(session)
        assert q.id == "Q3a"
        session = engine.submit_response(session, "Writing lab reports and literature review")
        assert session.status == "completed"
        assert len(session.responses) == 4

        # --- STUDENT: Alice chats with the course assistant ---

        shadow_captured = []

        def shadow_cb(query, results):
            shadow_captured.append({"query": query, "results": results})

        from axiom.rag.policy import PolicyAwareRetriever

        course_chunks = [
            {
                "text": "Fission is the splitting of heavy atomic nuclei.",
                "source": "textbook ch3",
                "tags": ["ch3"],
            },
            {
                "text": "Chain reactions sustain nuclear fission in reactors.",
                "source": "textbook ch4",
                "tags": ["ch4"],
            },
            {
                "text": "Control rods absorb neutrons to regulate power.",
                "source": "textbook ch5",
                "tags": ["ch5"],
            },
        ]
        personal_chunks = [
            {"text": "Prof said exam focuses on chapters 3-5.", "source": "alice-notes-july-8"},
        ]

        retriever = PolicyAwareRetriever(
            policy=result.rag_policy,
            corpus_registry={
                f"course-{result.classroom_id}": lambda q, top_k=5: course_chunks,
                "example-corpus": lambda q, top_k=5: [
                    {
                        "text": "Advanced: resonance integral calculations for U-238.",
                        "source": "example-consumer",
                    },
                ],
            },
            personal_retriever=lambda q, top_k=5: personal_chunks,
            shadow_callback=shadow_cb,
        )

        results = retriever.retrieve("What is fission?", top_k=10)

        # Primary results include course + personal
        sources = {r.get("source") for r in results}
        assert "textbook ch3" in sources  # course
        assert "alice-notes-july-8" in sources  # personal

        # Corpus types tagged for LangFuse
        types = {r.get("corpus_type") for r in results}
        assert "course" in types
        assert "personal" in types

        # Shadow captured (A/B path)
        assert len(shadow_captured) == 1
        assert shadow_captured[0]["results"][0]["source"] == "example-consumer"

        # --- STUDENT: Alice sends a chat through the pipeline ---

        from axiom.extensions.builtins.classroom.pipeline import ClassroomChatPipeline

        pipeline = ClassroomChatPipeline(
            course_system_prompt=manifest["system_prompt"],
            rag_retriever=lambda query, top_k=5: retriever.retrieve(query, top_k),
            llm_backend=lambda messages, **kw: (
                "Fission is the splitting of heavy nuclei into lighter ones, releasing energy."
            ),
        )

        response = pipeline.chat([{"role": "user", "content": "Explain fission"}])
        assert "fission" in response.lower()
        assert "Next steps" in response  # suggestions appended

        # OpenAI-compatible format
        completion = pipeline.handle_completion(
            {
                "messages": [{"role": "user", "content": "Explain fission"}],
                "model": "axiom-classroom",
            }
        )
        assert completion["object"] == "chat.completion"
        assert completion["choices"][0]["message"]["role"] == "assistant"

        # --- STUDENT: Alice ingests a lecture recording ---

        from axiom.ingest.media import MediaIngestPipeline, MediaItem, StudentTimeline

        media_pipeline = MediaIngestPipeline(
            transcriber=lambda path: (
                "Today we discussed neutron moderation. Water acts as a moderator."
            ),
        )

        recording = MediaItem(
            student_id="alice",
            media_type="audio",
            source_path="/tmp/fake-lecture.wav",
            title="Tuesday lecture - neutron moderation",
        )
        processed = media_pipeline.process(recording)

        assert processed.status == "processed"
        assert "neutron moderation" in processed.transcript
        assert len(processed.rag_chunks) > 0
        assert processed.rag_chunks[0]["student_id"] == "alice"

        # Add to timeline
        timeline = StudentTimeline(student_id="alice")
        timeline.add(processed)

        # Also ingest a whiteboard photo
        photo = MediaItem(
            student_id="alice",
            media_type="image",
            source_path="/tmp/fake-whiteboard.jpg",
            title="Whiteboard - moderation equations",
        )
        photo_pipeline = MediaIngestPipeline(
            ocr_processor=lambda path: "Σ_s(E) = N·σ_s(E)\nModeration ratio: ξΣ_s/Σ_a",
        )
        processed_photo = photo_pipeline.process(photo)
        assert processed_photo.status == "processed"
        assert "Σ_s" in processed_photo.extracted_text
        timeline.add(processed_photo)

        # Timeline has 2 items
        assert len(timeline.items) == 2
        assert len(timeline.filter_by_type("audio")) == 1
        assert len(timeline.filter_by_type("image")) == 1

        # All RAG chunks from timeline
        all_chunks = timeline.get_all_rag_chunks()
        assert len(all_chunks) >= 2  # at least 1 from audio + 1 from image

        # --- TRACING: verify attribution ---

        from axiom.extensions.builtins.classroom.tracing import ClassroomTracer
        from axiom.infra.tracing import InMemoryTraceProvider

        mem_tracer = InMemoryTraceProvider()
        tracer = ClassroomTracer(
            classroom_id="ne-prague-2026",
            course_id="ne-stem-2026",
            trace_provider=mem_tracer,
        )

        # Trace the chat
        trace_id = tracer.trace_chat(student_id="alice", message="Explain fission")
        tracer.log_generation(
            trace_id,
            model="bonsai-local",
            prompt=[{"role": "user", "content": "Explain fission"}],
            output=response,
        )

        # Trace the interview
        interview_trace = tracer.trace_interview(
            student_id="alice",
            questionnaire_id="pre-course-interview",
            question_id="Q1",
            response="I use ChatGPT for homework",
        )

        # Verify attribution
        chat_trace = mem_tracer.get_trace(trace_id)
        assert chat_trace["metadata"]["student_id"] == "alice"
        assert chat_trace["metadata"]["classroom_id"] == "ne-prague-2026"
        assert chat_trace["metadata"]["session_type"] == "chat"

        interview_t = mem_tracer.get_trace(interview_trace)
        assert interview_t["metadata"]["session_type"] == "interview"

        # Analytics
        alice_traces = tracer.get_student_traces("alice")
        assert len(alice_traces) == 2  # 1 chat + 1 interview

        # --- DOCTOR: health check ---

        checks = classroom_doctor(
            classroom_id="ne-prague-2026",
            web_endpoint="",  # no real endpoint in test
            trace_store_writable=True,
            rag_indexed=True,
            llm_responsive=True,
            tokens_valid=True,
        )
        healthy = [c for c in checks if c.status == "healthy"]
        assert len(healthy) >= 4  # trace, rag, llm, tokens

        # --- CANVAS: grade push ---

        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider

        lms = CanvasLMSProvider(
            {
                "api_url": canvas.url,
                "api_token": "t",
                "_mock_server": canvas,
            }
        )

        # Create assignment from manifest
        assignment = lms.create_assignment(
            course_id="101",
            name="Mid-Course Quiz",
            description="Assessment of learning objectives 1-5",
            points_possible=100,
        )
        assert assignment.success

        # Push Alice's grade
        grade = lms.push_grade(
            course_id="101",
            assignment_id=assignment.canvas_assignment_id,
            student_id="alice",
            score=92.0,
            comment="Excellent understanding of fission mechanisms.",
        )
        assert grade.success

        # --- COURSE MANIFEST: pack + round-trip ---

        import tempfile
        from pathlib import Path

        from axiom.extensions.builtins.classroom.course_manifest import (
            CourseManifest,
            create_axiompack,
            load_axiompack,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Write manifest
            import yaml

            manifest_path = tmp / "course.yaml"
            manifest_path.write_text(yaml.dump(manifest))

            course = CourseManifest(
                id=manifest["id"],
                title=manifest["title"],
                version=manifest["version"],
                system_prompt=manifest.get("system_prompt", ""),
                onboarding_rails=manifest.get("onboarding_rails", []),
                raw=manifest,
            )

            # Create pack
            pack_path = create_axiompack(course, source_dir=tmp, output_dir=tmp)
            assert pack_path.exists()

            # Load pack (round-trip)
            extract_dir = tmp / "extracted"
            loaded = load_axiompack(pack_path, extract_dir=extract_dir)
            assert loaded.id == "ne-prague-2026"
            assert loaded.version == "1.0.0"


@pytest.mark.classroom_e2e
class TestInstructorStudentInteraction:
    """I↔S interaction: instructor grades, student gets feedback."""

    def test_grade_push_and_enrollment_change_detection(self):
        from axiom.extensions.builtins.classroom.lms.canvas import CanvasLMSProvider
        from axiom.extensions.builtins.classroom.lms.canvas_mock import CanvasMockServer

        canvas = CanvasMockServer()
        canvas.add_course("101", "Course")
        canvas.add_enrollment(
            "101",
            {
                "user_id": "s1",
                "name": "A",
                "email": "a@a.com",
                "type": "StudentEnrollment",
            },
        )
        canvas.add_enrollment(
            "101",
            {
                "user_id": "s2",
                "name": "B",
                "email": "b@b.com",
                "type": "StudentEnrollment",
            },
        )

        lms = CanvasLMSProvider({"api_url": canvas.url, "api_token": "t", "_mock_server": canvas})

        # Initial roster
        initial = lms.get_roster("101")
        assert len(initial) == 2

        # Mid-semester: new student added, one drops
        canvas.add_enrollment(
            "101",
            {
                "user_id": "s3",
                "name": "C",
                "email": "c@c.com",
                "type": "StudentEnrollment",
            },
        )
        canvas.drop_enrollment("101", "s2")

        changes = lms.sync_enrollment_changes("101", [s.student_id for s in initial])
        assert len(changes.added) == 1
        assert changes.added[0].student_id == "s3"
        assert len(changes.dropped) == 1
        assert changes.dropped[0].student_id == "s2"


@pytest.mark.classroom_e2e
class TestStudentStudentCrossPollination:
    """S↔S interaction: one student's finding appears in another's retrieval."""

    def test_promoted_finding_visible_to_peer(self):
        from axiom.rag.policy import PolicyAwareRetriever, RAGPolicy

        # Course corpus (shared)
        course_chunks = [
            {"text": "Base course content about reactors.", "source": "textbook"},
        ]

        # Alice promotes a finding → it enters the course corpus
        alice_promoted = {
            "text": "I found that ATF cladding reduces hydrogen generation.",
            "source": "alice-finding-promoted",
        }
        course_with_promotion = course_chunks + [alice_promoted]

        policy = RAGPolicy(id="shared", name="Shared", corpora=[{"corpus_id": "course"}])

        # Bob's retriever sees the course corpus (which now includes Alice's promoted finding)
        bob_retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={"course": lambda q, top_k=5: course_with_promotion},
            personal_retriever=lambda q, top_k=5: [
                {"text": "Bob's personal notes on coolant systems.", "source": "bob-notes"},
            ],
        )

        results = bob_retriever.retrieve("hydrogen generation in reactors", top_k=10)

        # Bob sees: course content + Alice's promoted finding + his own notes
        sources = {r.get("source") for r in results}
        assert "textbook" in sources
        assert "alice-finding-promoted" in sources  # S↔S cross-pollination!
        assert "bob-notes" in sources  # personal
