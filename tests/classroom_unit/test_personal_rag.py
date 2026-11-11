# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for personal RAG integration into the PolicyAwareRetriever.

Personal corpus is ALWAYS queried alongside course corpus. Results
are tagged with corpus_type for LangFuse disambiguation. Course-level
void rules do NOT affect the personal corpus.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class TestPersonalRAGInRetriever:
    def test_personal_corpus_always_included(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        policy = RAGPolicy(
            id="course-only",
            name="Course Only",
            corpora=[{"corpus_id": "course"}],
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "course": lambda q, top_k=5: [
                    {"text": "Course fact", "source": "textbook", "corpus_type": "course"}
                ],
            },
            personal_retriever=lambda q, top_k=5: [
                {"text": "My lecture notes say X", "source": "my-notes", "corpus_type": "personal"}
            ],
        )

        results = retriever.retrieve("fission", top_k=10)

        sources = {r.get("corpus_type") for r in results}
        assert "course" in sources
        assert "personal" in sources
        assert len(results) == 2

    def test_personal_not_affected_by_void_rules(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        now = datetime.now(UTC)
        policy = RAGPolicy(
            id="quiz",
            name="Quiz",
            corpora=[{"corpus_id": "course"}],
            void_rules=[
                {
                    "id": "v1",
                    "corpus_id": "course",
                    "subset_filter": "*",  # void EVERYTHING in course
                    "reason": "Full quiz lockdown",
                    "starts_at": (now - timedelta(hours=1)).isoformat(),
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                }
            ],
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "course": lambda q, top_k=5: [
                    {"text": "Course fact", "source": "textbook", "tags": ["ch1"]}
                ],
            },
            personal_retriever=lambda q, top_k=5: [
                {"text": "My personal notes", "source": "my-notes", "corpus_type": "personal"}
            ],
        )

        results = retriever.retrieve("question", top_k=10)

        # Course voided (full *), but personal survives
        assert not any(r.get("source") == "textbook" for r in results)
        assert any(r.get("corpus_type") == "personal" for r in results)

    def test_no_personal_retriever_still_works(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        policy = RAGPolicy(id="basic", name="Basic", corpora=[{"corpus_id": "course"}])

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "course": lambda q, top_k=5: [{"text": "Fact", "source": "course"}],
            },
            # no personal_retriever
        )

        results = retriever.retrieve("q", top_k=5)
        assert len(results) == 1

    def test_results_tagged_with_corpus_type(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        policy = RAGPolicy(id="t", name="T", corpora=[{"corpus_id": "c"}])

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "c": lambda q, top_k=5: [{"text": "Course", "source": "c"}],
            },
            personal_retriever=lambda q, top_k=5: [{"text": "Personal", "source": "p"}],
        )

        results = retriever.retrieve("q", top_k=10)

        # Course results should get corpus_type tagged
        course_r = [r for r in results if r.get("source") == "c"]
        personal_r = [r for r in results if r.get("source") == "p"]

        assert len(course_r) == 1
        assert course_r[0].get("corpus_type") == "course"
        assert len(personal_r) == 1
        assert personal_r[0].get("corpus_type") == "personal"
