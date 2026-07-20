# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.memory.graph — Layer 2 minimum-viable concept graph.

Per spec-memory.md §5 + ADR-033 Stage 2. Tests cover:

- Concept identity (deterministic across instances) — federation
  invariant for shared concept_id namespace.
- SQLiteConceptGraph upsert + neighbors + traversal contract.
- DeterministicTextExtractor produces stable, interpretable output.
- ExtractorRegistry enforces classification-aware selection (the
  C7 compliance invariant: CUI fragments never see external-provider
  extractors with insufficient capability).
- rebuild_graph_from_log satisfies I8 (graph fully derivable from L1).
"""

from __future__ import annotations

import pytest

from axiom.memory.fragment import CognitiveType, create_fragment
from axiom.memory.graph import (
    Concept,
    ConceptEdge,
    DeterministicTextExtractor,
    ExtractorCapability,
    ExtractorRegistry,
    GraphQuery,
    SQLiteConceptGraph,
    canonical_concept_id,
    rebuild_graph_from_log,
)

# ---------------------------------------------------------------------------
# Concept identity — federation invariant
# ---------------------------------------------------------------------------


class TestCanonicalConceptId:
    """Same name → same id, regardless of who/where extracts. Federation
    relies on this; without it, cross-cohort concept joins need a global
    registry."""

    def test_same_name_same_id(self):
        a = canonical_concept_id("criticality")
        b = canonical_concept_id("criticality")
        assert a == b

    def test_case_and_whitespace_normalized(self):
        a = canonical_concept_id("Reactor   Physics")
        b = canonical_concept_id("reactor physics")
        c = canonical_concept_id("REACTOR PHYSICS")
        assert a == b == c

    def test_different_names_different_ids(self):
        assert (
            canonical_concept_id("criticality")
            != canonical_concept_id("control rod")
        )


# ---------------------------------------------------------------------------
# SQLiteConceptGraph — write, read, traversal
# ---------------------------------------------------------------------------


@pytest.fixture
def graph(tmp_path):
    return SQLiteConceptGraph(tmp_path / "graph.db")


class TestSQLiteConceptGraphCRUD:
    def test_upsert_then_get_concept(self, graph):
        c = Concept(
            concept_id=canonical_concept_id("criticality"),
            canonical_name="criticality",
            extracted_from=["frag-1"],
            confidence=0.7,
        )
        graph.upsert_concept(c)
        loaded = graph.get_concept(c.concept_id)
        assert loaded == c

    def test_upsert_merges_extracted_from(self, graph):
        cid = canonical_concept_id("criticality")
        graph.upsert_concept(Concept(
            concept_id=cid, canonical_name="criticality",
            extracted_from=["frag-1"], confidence=0.6,
        ))
        graph.upsert_concept(Concept(
            concept_id=cid, canonical_name="criticality",
            extracted_from=["frag-2"], confidence=0.8,
        ))
        loaded = graph.get_concept(cid)
        assert sorted(loaded.extracted_from) == ["frag-1", "frag-2"]
        # Confidence keeps the max — strongest evidence wins.
        assert loaded.confidence == 0.8

    def test_get_unknown_returns_none(self, graph):
        assert graph.get_concept("nope") is None

    def test_concept_count_and_edge_count(self, graph):
        graph.upsert_concept(Concept(
            concept_id=canonical_concept_id("a"),
            canonical_name="a",
        ))
        graph.upsert_concept(Concept(
            concept_id=canonical_concept_id("b"),
            canonical_name="b",
        ))
        graph.upsert_edge(ConceptEdge(
            from_concept=canonical_concept_id("a"),
            to_concept=canonical_concept_id("b"),
            edge_type="co_occurs",
        ))
        assert graph.concept_count() == 2
        assert graph.edge_count() == 1


class TestSQLiteConceptGraphTraversal:
    def _seed(self, graph):
        # a — b — c   (chain)
        # a — d       (branch)
        for name in ["a", "b", "c", "d"]:
            graph.upsert_concept(Concept(
                concept_id=canonical_concept_id(name),
                canonical_name=name,
            ))
        for f, t in [("a", "b"), ("b", "c"), ("a", "d")]:
            graph.upsert_edge(ConceptEdge(
                from_concept=canonical_concept_id(f),
                to_concept=canonical_concept_id(t),
                edge_type="co_occurs",
                evidence=["frag-1"],
            ))

    def test_neighbors_one_hop(self, graph):
        self._seed(graph)
        result = graph.neighbors(canonical_concept_id("a"), hops=1)
        names = sorted(c.canonical_name for c in result)
        assert names == ["b", "d"]

    def test_neighbors_two_hops_includes_chain_end(self, graph):
        self._seed(graph)
        result = graph.neighbors(canonical_concept_id("a"), hops=2)
        names = sorted(c.canonical_name for c in result)
        # 'c' is reachable from 'a' via 'b' at hop 2.
        assert "c" in names

    def test_query_seeds_and_limit(self, graph):
        self._seed(graph)
        result = graph.query(GraphQuery(
            seed_concepts=frozenset({canonical_concept_id("a")}),
            max_hops=2,
            limit=10,
        ))
        names = sorted(c.canonical_name for c in result)
        assert "b" in names
        assert "c" in names

    def test_edge_type_filter(self, graph):
        self._seed(graph)
        graph.upsert_edge(ConceptEdge(
            from_concept=canonical_concept_id("a"),
            to_concept=canonical_concept_id("b"),
            edge_type="depends_on",
        ))
        # Restrict to depends_on; only that one direction shows up.
        result = graph.neighbors(
            canonical_concept_id("a"),
            hops=1,
            edge_types=frozenset({"depends_on"}),
        )
        assert [c.canonical_name for c in result] == ["b"]


# ---------------------------------------------------------------------------
# DeterministicTextExtractor — text → concepts + co-occurrence edges
# ---------------------------------------------------------------------------


class TestDeterministicTextExtractor:
    def test_extracts_question_words(self):
        extractor = DeterministicTextExtractor()
        frag = create_fragment(
            content={
                "event_time": "2026-04-26T10:00:00+00:00",
                "question": "What is reactor criticality?",
                "had_answer": True, "citations_count": 1,
            },
            cognitive_type="episodic",
            principal_id="alice", agents=set(), resources=set(),
        )
        concepts = extractor.extract(frag)
        names = sorted(c.canonical_name for c in concepts)
        # Stop-words filtered; question, was, etc. removed.
        assert "reactor" in names
        assert "criticality" in names
        assert "what" not in names
        assert "the" not in names

    def test_concepts_carry_back_to_fragment(self):
        extractor = DeterministicTextExtractor()
        frag = create_fragment(
            content={
                "event_time": "2026-04-26T10:00:00+00:00",
                "question": "criticality",
                "had_answer": True, "citations_count": 1,
            },
            cognitive_type="episodic",
            principal_id="alice", agents=set(), resources=set(),
        )
        concepts = extractor.extract(frag)
        for c in concepts:
            assert frag.id in c.extracted_from

    def test_co_occurrence_edges_are_symmetric_pairs(self, graph):
        extractor = DeterministicTextExtractor()
        frag = create_fragment(
            content={
                "event_time": "2026-04-26T10:00:00+00:00",
                "question": "reactor criticality control",
                "had_answer": True, "citations_count": 1,
            },
            cognitive_type="episodic",
            principal_id="alice", agents=set(), resources=set(),
        )
        edges = extractor.link(frag, graph)
        # 3 concepts → 3 unordered pairs = 3 co_occurs edges.
        assert len(edges) == 3
        for e in edges:
            assert e.edge_type == "co_occurs"
            assert frag.id in e.evidence

    def test_handles_episodic_and_resource(self):
        extractor = DeterministicTextExtractor()
        assert CognitiveType.EPISODIC in extractor.handles
        assert CognitiveType.RESOURCE in extractor.handles

    def test_capability_max_classification_is_top_secret(self):
        """Local deterministic extractor — no provider, no exfil — is
        safe at any classification level."""
        extractor = DeterministicTextExtractor()
        assert extractor.capability.max_classification == "top_secret"
        assert extractor.capability.runs_on == "local"
        assert extractor.capability.provider_id is None


# ---------------------------------------------------------------------------
# ExtractorRegistry — classification-gated invocation (C7 compliance)
# ---------------------------------------------------------------------------


class _FakeExternalExtractor:
    """Stand-in for an LLM extractor that calls an external provider —
    must NEVER see CUI / classified content per spec-classification-boundary
    Invariant 'LLM operations are domain-scoped and must never cross.'"""

    def __init__(self):
        self.invocations = []
        self.capability = ExtractorCapability(
            name="fake_external_llm",
            runs_on="external_provider",
            provider_id="openai",
            logs_to=("openai_metrics",),
            max_classification="unclassified",  # cap: only unclassified
        )

    @property
    def handles(self):
        return frozenset({CognitiveType.EPISODIC})

    def extract(self, fragment):
        self.invocations.append(fragment.id)
        return []

    def link(self, fragment, existing):
        return []


class TestExtractorRegistryClassificationGate:
    """Compliance C7 (spec-memory I11): a CUI fragment MUST NEVER receive
    an extractor whose capability.max_classification < cui."""

    def test_external_extractor_skipped_for_cui_fragment(self, graph):
        import dataclasses

        from axiom.vega.federation.policy import ClassificationStamp

        external = _FakeExternalExtractor()
        local = DeterministicTextExtractor()
        registry = ExtractorRegistry(graph=graph)
        registry.register(external)
        registry.register(local)

        cui_frag = dataclasses.replace(
            create_fragment(
                content={
                    "event_time": "2026-04-26T10:00:00+00:00",
                    "question": "classified topic",
                    "had_answer": True, "citations_count": 1,
                },
                cognitive_type="episodic",
                principal_id="alice", agents=set(), resources=set(),
            ),
            classification=ClassificationStamp(level="cui"),
        )

        summary = registry.run_for_fragment(cui_frag)

        # External extractor was skipped — never invoked.
        assert external.invocations == []
        assert "fake_external_llm" in summary["extractors_skipped_classification"]
        # Local deterministic extractor ran fine.
        assert "deterministic_text" in summary["extractors_run"]
        assert summary["concepts"] > 0

    def test_unclassified_external_extractor_runs(self, graph):
        external = _FakeExternalExtractor()
        registry = ExtractorRegistry(graph=graph)
        registry.register(external)

        unclass_frag = create_fragment(
            content={
                "event_time": "2026-04-26T10:00:00+00:00",
                "question": "open topic",
                "had_answer": True, "citations_count": 1,
            },
            cognitive_type="episodic",
            principal_id="alice", agents=set(), resources=set(),
        )

        registry.run_for_fragment(unclass_frag)
        # No CUI marking → external extractor allowed to run.
        assert external.invocations == [unclass_frag.id]

    def test_secret_fragment_only_local_extractors(self, graph):
        import dataclasses

        from axiom.vega.federation.policy import ClassificationStamp

        external = _FakeExternalExtractor()
        local = DeterministicTextExtractor()
        registry = ExtractorRegistry(graph=graph)
        registry.register(external)
        registry.register(local)

        secret_frag = dataclasses.replace(
            create_fragment(
                content={
                    "event_time": "2026-04-26T10:00:00+00:00",
                    "question": "secret topic",
                    "had_answer": True, "citations_count": 1,
                },
                cognitive_type="episodic",
                principal_id="alice", agents=set(), resources=set(),
            ),
            classification=ClassificationStamp(level="secret"),
        )

        summary = registry.run_for_fragment(secret_frag)
        assert external.invocations == []
        assert "deterministic_text" in summary["extractors_run"]


# ---------------------------------------------------------------------------
# Replay invariant — graph fully derivable from L1 (I8)
# ---------------------------------------------------------------------------


class TestEvidenceTableScaling:
    """Stage-3-of-L2 refactor: edge evidence + concept extracted_from
    storage must NOT scale O(N) per write. Surfaced by integrated
    benchmark — original schema serialized whole JSON arrays per
    upsert; refactored to indexed evidence rows for O(1) inserts."""

    def test_repeated_writes_to_same_edge_stay_bounded(self, graph):
        """Edge upsert cost must not scale with prior evidence count —
        O(1) indexed-row inserts, not O(N) whole-array reserialization.

        Measured via the MINIMUM per-upsert time in an early vs a late
        window. The minimum reflects uncontended execution and is robust
        to shared-runner scheduling jitter (GC pauses, co-tenant load)
        that inflates wall-clock *sums* — the prior single-window-ratio
        form flaked on loaded CI. The late window carries ~10x the prior
        evidence of the early one, so a genuine O(N) regression (the old
        JSON-blob upsert reserialized the whole array — ~3-4x at 250
        items, and far more here) stands out well beyond the jitter bound,
        while an O(1) insert keeps the min-ratio ~1x."""
        import time

        a = canonical_concept_id("a")
        b = canonical_concept_id("b")
        graph.upsert_concept(Concept(concept_id=a, canonical_name="a"))
        graph.upsert_concept(Concept(concept_id=b, canonical_name="b"))

        def min_upsert_time(tag, n=40):
            best = float("inf")
            for i in range(n):
                t0 = time.perf_counter()
                graph.upsert_edge(ConceptEdge(
                    from_concept=a, to_concept=b, edge_type="co_occurs",
                    evidence=[f"frag-{tag}-{i}"],
                ))
                best = min(best, time.perf_counter() - t0)
            return best

        # Early window: ~0-40 prior evidence rows.
        early_min = min_upsert_time("early")

        # Grow prior evidence ~10x so an O(N) upsert would be sharply
        # slower even at its fastest; an O(1) insert stays flat.
        for i in range(400):
            graph.upsert_edge(ConceptEdge(
                from_concept=a, to_concept=b, edge_type="co_occurs",
                evidence=[f"frag-bulk-{i}"],
            ))

        # Late window: ~440-480 prior evidence rows.
        late_min = min_upsert_time("late")

        # O(1): late_min ~= early_min. An O(N) upsert over ~10x the
        # evidence would be many-fold slower at the min; 4x catches that
        # while the min-of-N sampling absorbs runner jitter.
        assert late_min < early_min * 4.0, (
            f"Edge upsert appears to scale with prior evidence count: "
            f"early_min={early_min * 1e6:.0f}us, "
            f"late_min={late_min * 1e6:.0f}us"
        )

    def test_get_concept_returns_full_extracted_from(self, graph):
        """API contract preserved — get_concept returns Concept with
        accumulated extracted_from list, regardless of storage shape."""
        cid = canonical_concept_id("x")
        for fid in [f"frag-{i}" for i in range(20)]:
            graph.upsert_concept(Concept(
                concept_id=cid, canonical_name="x",
                extracted_from=[fid], confidence=0.5,
            ))

        loaded = graph.get_concept(cid)
        assert loaded is not None
        # All 20 fragment_ids accumulated.
        assert sorted(loaded.extracted_from) == sorted(
            f"frag-{i}" for i in range(20)
        )

    def test_concept_extracted_from_dedupes(self, graph):
        """Same fragment writing the same concept twice doesn't double-
        count; extracted_from stays a set."""
        cid = canonical_concept_id("y")
        for _ in range(5):
            graph.upsert_concept(Concept(
                concept_id=cid, canonical_name="y",
                extracted_from=["frag-1"],
            ))
        loaded = graph.get_concept(cid)
        assert loaded.extracted_from == ["frag-1"]


class TestReplayInvariant:
    """rebuild_graph_from_log MUST produce equivalent state from a
    fresh empty graph given the same fragments."""

    def test_rebuild_produces_same_concept_set(self, tmp_path):
        # Build graph A by running extractors on fragments.
        graph_a = SQLiteConceptGraph(tmp_path / "a.db")
        registry_a = ExtractorRegistry(graph=graph_a)
        registry_a.register(DeterministicTextExtractor())

        fragments = []
        for q in ["criticality reactor", "control rods", "criticality control"]:
            f = create_fragment(
                content={
                    "event_time": "2026-04-26T10:00:00+00:00",
                    "question": q,
                    "had_answer": True, "citations_count": 1,
                },
                cognitive_type="episodic",
                principal_id="alice", agents=set(), resources=set(),
            )
            fragments.append(f)
            registry_a.run_for_fragment(f)

        # Replay into graph B from scratch.
        graph_b = SQLiteConceptGraph(tmp_path / "b.db")
        registry_b = ExtractorRegistry(graph=graph_b)
        registry_b.register(DeterministicTextExtractor())
        rebuild_graph_from_log(
            graph=graph_b, registry=registry_b, fragments=fragments,
        )

        a_concepts = sorted(c.canonical_name for c in graph_a.all_concepts())
        b_concepts = sorted(c.canonical_name for c in graph_b.all_concepts())
        assert a_concepts == b_concepts
        assert graph_a.edge_count() == graph_b.edge_count()
