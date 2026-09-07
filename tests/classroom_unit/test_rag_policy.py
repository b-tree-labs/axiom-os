# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for dynamic RAG routing with policy-driven corpus selection.

The RAGPolicy determines which corpora a query hits, what's voided,
and whether a shadow path runs in parallel for A/B comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# 1. POLICY CREATION
# ---------------------------------------------------------------------------


class TestRAGPolicyCreation:
    def test_create_course_only_policy(self):
        from axiom.extensions.builtins.classroom.rag_policy import RAGPolicy

        policy = RAGPolicy(
            id="prague-ne-primary",
            name="Course Materials Only",
            corpora=[{"corpus_id": "course-ne-prague-2026", "tier_filter": "public"}],
        )
        assert policy.id == "prague-ne-primary"
        assert len(policy.corpora) == 1
        assert policy.shadow_config is None

    def test_create_multi_corpus_policy(self):
        from axiom.extensions.builtins.classroom.rag_policy import RAGPolicy

        policy = RAGPolicy(
            id="full",
            name="Course + Institutional",
            corpora=[
                {"corpus_id": "course-ne-prague-2026", "weight": 1.0},
                {"corpus_id": "ut-institutional", "weight": 0.5},
            ],
        )
        assert len(policy.corpora) == 2

    def test_create_with_shadow(self):
        from axiom.extensions.builtins.classroom.rag_policy import RAGPolicy

        policy = RAGPolicy(
            id="ab-test",
            name="A/B Test",
            corpora=[{"corpus_id": "course-only"}],
            shadow_config={"shadow_corpus_id": "example-corpus", "capture_to": "langfuse"},
        )
        assert policy.shadow_config is not None
        assert policy.shadow_config["shadow_corpus_id"] == "example-corpus"


# ---------------------------------------------------------------------------
# 2. VOID RULES
# ---------------------------------------------------------------------------


class TestVoidRules:
    def test_void_excludes_subset(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        chunks = [
            {"text": "Chapter 5 content", "source": "textbook", "tags": ["ch5"]},
            {"text": "Chapter 6 content", "source": "textbook", "tags": ["ch6"]},
            {"text": "Lecture 1 notes", "source": "lectures", "tags": ["lec1"]},
        ]

        def mock_corpus(query, top_k=5):
            return chunks

        policy = RAGPolicy(
            id="quiz-mode",
            name="Quiz",
            corpora=[{"corpus_id": "course"}],
            void_rules=[
                {
                    "id": "v1",
                    "corpus_id": "course",
                    "subset_filter": "tag:ch5",
                    "reason": "Quiz on chapter 5",
                    "starts_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
            ],
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={"course": mock_corpus},
        )
        results = retriever.retrieve("chapter 5 question", top_k=5)

        # Chapter 5 should be voided
        assert not any("Chapter 5" in r["text"] for r in results)
        # Chapter 6 and lecture should remain
        assert any("Chapter 6" in r["text"] for r in results)
        assert any("Lecture 1" in r["text"] for r in results)

    def test_expired_void_does_not_exclude(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        chunks = [
            {"text": "Chapter 5 content", "source": "textbook", "tags": ["ch5"]},
        ]

        policy = RAGPolicy(
            id="expired",
            name="Expired void",
            corpora=[{"corpus_id": "course"}],
            void_rules=[
                {
                    "id": "v1",
                    "corpus_id": "course",
                    "subset_filter": "tag:ch5",
                    "reason": "Old quiz",
                    "starts_at": (datetime.now(UTC) - timedelta(hours=5)).isoformat(),
                    "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                }
            ],
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={"course": lambda q, top_k=5: chunks},
        )
        results = retriever.retrieve("chapter 5", top_k=5)

        # Void expired → chapter 5 NOT excluded
        assert any("Chapter 5" in r["text"] for r in results)


# ---------------------------------------------------------------------------
# 3. MULTI-CORPUS MERGING
# ---------------------------------------------------------------------------


class TestMultiCorpusMerging:
    def test_merges_results_from_multiple_corpora(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        course_chunks = [{"text": "Course fact A", "source": "course"}]
        community_chunks = [{"text": "Community fact B", "source": "community"}]

        policy = RAGPolicy(
            id="merged",
            name="Merged",
            corpora=[
                {"corpus_id": "course", "weight": 1.0},
                {"corpus_id": "community", "weight": 0.5},
            ],
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "course": lambda q, top_k=5: course_chunks,
                "community": lambda q, top_k=5: community_chunks,
            },
        )
        results = retriever.retrieve("query", top_k=10)

        assert len(results) == 2
        sources = {r["source"] for r in results}
        assert "course" in sources
        assert "community" in sources

    def test_missing_corpus_skipped_gracefully(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        policy = RAGPolicy(
            id="partial",
            name="Partial",
            corpora=[
                {"corpus_id": "exists"},
                {"corpus_id": "missing"},
            ],
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "exists": lambda q, top_k=5: [{"text": "Found", "source": "exists"}],
            },
        )
        results = retriever.retrieve("query", top_k=5)

        assert len(results) == 1
        assert results[0]["source"] == "exists"


# ---------------------------------------------------------------------------
# 4. SHADOW PATH (A/B)
# ---------------------------------------------------------------------------


class TestShadowPath:
    def test_shadow_results_captured_separately(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        shadow_captured = []

        def shadow_capture(query, results):
            shadow_captured.append({"query": query, "results": results})

        policy = RAGPolicy(
            id="ab",
            name="A/B",
            corpora=[{"corpus_id": "course"}],
            shadow_config={
                "shadow_corpus_id": "full-example-corpus",
                "capture_to": "callback",
            },
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "course": lambda q, top_k=5: [{"text": "Course answer", "source": "course"}],
                "full-example-corpus": lambda q, top_k=5: [
                    {"text": "Full RAG answer", "source": "full"}
                ],
            },
            shadow_callback=shadow_capture,
        )

        primary = retriever.retrieve("question", top_k=5)

        # Primary returns course-only
        assert len(primary) == 1
        assert primary[0]["source"] == "course"

        # Shadow was captured separately
        assert len(shadow_captured) == 1
        assert shadow_captured[0]["results"][0]["source"] == "full"

    def test_shadow_failure_does_not_affect_primary(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        def broken_corpus(q, top_k=5):
            raise RuntimeError("Shadow corpus unavailable")

        policy = RAGPolicy(
            id="robust",
            name="Robust",
            corpora=[{"corpus_id": "course"}],
            shadow_config={"shadow_corpus_id": "broken", "capture_to": "callback"},
        )

        retriever = PolicyAwareRetriever(
            policy=policy,
            corpus_registry={
                "course": lambda q, top_k=5: [{"text": "Primary works", "source": "course"}],
                "broken": broken_corpus,
            },
        )

        # Should NOT raise — shadow failure is swallowed
        results = retriever.retrieve("query", top_k=5)
        assert len(results) == 1
        assert results[0]["text"] == "Primary works"


# ---------------------------------------------------------------------------
# 5. RUNTIME POLICY SWAP
# ---------------------------------------------------------------------------


class TestRuntimePolicySwap:
    def test_swap_policy_changes_results(self):
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        course_only = RAGPolicy(
            id="course-only",
            name="Course Only",
            corpora=[{"corpus_id": "course"}],
        )
        full = RAGPolicy(
            id="full",
            name="Full",
            corpora=[{"corpus_id": "course"}, {"corpus_id": "community"}],
        )

        registry = {
            "course": lambda q, top_k=5: [{"text": "Course", "source": "c"}],
            "community": lambda q, top_k=5: [{"text": "Community", "source": "m"}],
        }

        retriever = PolicyAwareRetriever(policy=course_only, corpus_registry=registry)
        assert len(retriever.retrieve("q", 5)) == 1

        retriever.set_policy(full)
        assert len(retriever.retrieve("q", 5)) == 2

    def test_policy_swap_is_immediate(self):
        """No restart needed — swap takes effect on next retrieve()."""
        from axiom.extensions.builtins.classroom.rag_policy import (
            PolicyAwareRetriever,
            RAGPolicy,
        )

        p1 = RAGPolicy(id="p1", name="P1", corpora=[{"corpus_id": "a"}])
        p2 = RAGPolicy(id="p2", name="P2", corpora=[{"corpus_id": "b"}])

        registry = {
            "a": lambda q, top_k=5: [{"text": "A", "source": "a"}],
            "b": lambda q, top_k=5: [{"text": "B", "source": "b"}],
        }

        retriever = PolicyAwareRetriever(policy=p1, corpus_registry=registry)
        assert retriever.retrieve("q", 5)[0]["source"] == "a"

        retriever.set_policy(p2)
        assert retriever.retrieve("q", 5)[0]["source"] == "b"
