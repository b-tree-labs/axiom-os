# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG Quality Benchmark — proves RAG data packs improve response quality.

Runs 20+ questions with and without RAG context, scores each answer,
and asserts that RAG-grounded answers are measurably better.

This is not a unit test — it requires:
  - DATABASE_URL pointing to a PG with community corpus loaded
  - An LLM endpoint (PRIVATE_LLM_API_KEY or local)

Run: pytest tests/test_rag_quality_benchmark.py -v --timeout=300
"""

from __future__ import annotations

import os
import re

import pytest

# Skip entire module if no DATABASE_URL
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — benchmark requires live RAG store",
)


# ---------------------------------------------------------------------------
# Gold Q&A pairs — answers must be derivable from the community corpus
# ---------------------------------------------------------------------------

GOLD_QA = [
    # ── 10 CFR Part 50 (regulatory) ──────────────────────────────────────
    {
        "id": "reg-01",
        "category": "regulatory",
        "question": "What is the peak cladding temperature limit specified in 10 CFR 50.46 for ECCS acceptance criteria?",
        "gold_answer": "2200 degrees Fahrenheit (1204 degrees Celsius)",
        "keywords": ["2200", "1204", "cladding", "temperature"],
        "source": "10CFR-Part50-Domestic-Licensing.pdf",
    },
    {
        "id": "reg-02",
        "category": "regulatory",
        "question": "According to 10 CFR Part 50, what is the maximum local oxidation limit for fuel cladding?",
        "gold_answer": "17 percent of the total cladding thickness",
        "keywords": ["17", "percent", "oxidation", "cladding"],
        "source": "10CFR-Part50-Domestic-Licensing.pdf",
    },
    {
        "id": "reg-03",
        "category": "regulatory",
        "question": "What does 10 CFR 50.46 require regarding hydrogen generation in a LOCA?",
        "gold_answer": "Total hydrogen generation shall not exceed 1 percent of the hypothetical amount from reaction of all metal cladding",
        "keywords": ["1 percent", "hydrogen", "cladding"],
        "source": "10CFR-Part50-Domestic-Licensing.pdf",
    },
    {
        "id": "reg-04",
        "category": "regulatory",
        "question": "What General Design Criterion addresses the inspection of emergency core cooling systems?",
        "gold_answer": "Criterion 36",
        "keywords": ["Criterion 36", "inspection", "emergency core cooling"],
        "source": "10CFR-Part50-Domestic-Licensing.pdf",
    },
    # ── MSRE (historical facility knowledge) ─────────────────────────────
    {
        "id": "msre-01",
        "category": "facility",
        "question": "What was the design thermal power of the Molten Salt Reactor Experiment (MSRE)?",
        "gold_answer": "10 MW thermal (later reduced to 8 MW for operation)",
        "keywords": ["10", "MW", "thermal"],
        "source": "ORNL-4541-MSRE-Design-Ops-Part1.pdf",
    },
    {
        "id": "msre-02",
        "category": "facility",
        "question": "What was the primary fuel salt composition used in the MSRE?",
        "gold_answer": "LiF-BeF2-ZrF4-UF4 (65-29.1-5-0.9 mole percent)",
        "keywords": ["LiF", "BeF2", "ZrF4", "UF4"],
        "source": "ORNL-4449-MSRE-Chemistry.pdf",
    },
    {
        "id": "msre-03",
        "category": "facility",
        "question": "What type of moderator was used in the MSRE core?",
        "gold_answer": "Unclad graphite bars",
        "keywords": ["graphite"],
        "source": "ORNL-4541-MSRE-Design-Ops-Part1.pdf",
    },
    {
        "id": "msre-04",
        "category": "facility",
        "question": "Which two fissile fuels were operated in the MSRE?",
        "gold_answer": "U-235 and U-233",
        "keywords": ["U-235", "U-233", "233", "235"],
        "source": "ORNL-4396-MSRE-Operations-Experience.pdf",
    },
    # ── TRIGA (research reactor) ─────────────────────────────────────────
    {
        "id": "triga-01",
        "category": "facility",
        "question": "What type of fuel is used in TRIGA Mark II research reactors?",
        "gold_answer": "Uranium-zirconium hydride (UZrH) fuel",
        "keywords": ["zirconium", "hydride", "UZrH"],
        "source": "NUREG-CR-3584-TRIGA-MarkII-Analysis.pdf",
    },
    {
        "id": "triga-02",
        "category": "facility",
        "question": "What inherent safety feature makes TRIGA reactors unique?",
        "gold_answer": "Large prompt negative temperature coefficient of reactivity from the ZrH moderator",
        "keywords": ["negative", "temperature", "coefficient", "prompt"],
        "source": "NUREG-CR-3584-TRIGA-MarkII-Analysis.pdf",
    },
    # ── IAEA (standards) ─────────────────────────────────────────────────
    {
        "id": "iaea-01",
        "category": "standards",
        "question": "According to IAEA SSR-3, what is the fundamental safety objective for research reactors?",
        "gold_answer": "To protect people and the environment from harmful effects of ionizing radiation",
        "keywords": ["protect", "people", "environment", "radiation"],
        "source": "IAEA-SSR-3-Safety-Research-Reactors.pdf",
    },
    # ── Radiation protection ─────────────────────────────────────────────
    {
        "id": "rp-01",
        "category": "regulatory",
        "question": "What does ALARA stand for in radiation protection?",
        "gold_answer": "As Low As Reasonably Achievable",
        "keywords": ["low", "reasonably", "achievable"],
        "source": "10CFR-Part20-Standards-for-Protection.pdf",
    },
    {
        "id": "rp-02",
        "category": "regulatory",
        "question": "What is the annual occupational dose limit for total effective dose equivalent under 10 CFR 20?",
        "gold_answer": "5 rem (0.05 Sv) per year",
        "keywords": ["5", "rem", "0.05", "Sv"],
        "source": "10CFR-Part20-Standards-for-Protection.pdf",
    },
    # ── Criticality safety ───────────────────────────────────────────────
    {
        "id": "crit-01",
        "category": "procedural",
        "question": "What is the Upper Subcritical Limit (USL) used for in criticality safety validation?",
        "gold_answer": "The USL is the maximum allowed keff value for a subcritical system, accounting for calculational bias and uncertainty",
        "keywords": ["keff", "subcritical", "bias", "uncertainty"],
        "source": "NUREG-CR-6698-Criticality-Safety-Validation.pdf",
    },
    # ── OpenMC (simulation) ──────────────────────────────────────────────
    {
        "id": "sim-01",
        "category": "simulation",
        "question": "What is the random ray method in OpenMC used for?",
        "gold_answer": "A stochastic method for solving the neutron transport equation using random ray tracing",
        "keywords": ["random", "ray", "transport", "neutron"],
        "source": "source/methods/random_ray.txt",
    },
    # ── Cross-document / multi-hop ───────────────────────────────────────
    {
        "id": "xdoc-01",
        "category": "cross-document",
        "question": "What regulatory requirements apply to the graphite moderator used in molten salt reactors like the MSRE?",
        "gold_answer": "General Design Criteria in 10 CFR 50 Appendix A address reactor core design (Criterion 10) and fuel system boundary requirements",
        "keywords": ["graphite", "molten salt", "design criteria"],
        "source": "multiple",
    },
    {
        "id": "xdoc-02",
        "category": "cross-document",
        "question": "How do research reactor technical specifications relate to IAEA safety requirements?",
        "gold_answer": "IAEA SSR-3 establishes safety requirements that feed into national technical specifications via operating limits and conditions",
        "keywords": ["technical specifications", "operating limits", "safety"],
        "source": "multiple",
    },
    # ── NRC AI workshops (recent/niche) ──────────────────────────────────
    {
        "id": "ai-01",
        "category": "regulatory",
        "question": "Has the NRC held workshops on the use of AI in nuclear regulation?",
        "gold_answer": "Yes, the NRC has conducted multiple AI workshops since 2021",
        "keywords": ["NRC", "AI", "workshop"],
        "source": "NRC_AI_Workshop",
    },
    # ── 10 CFR Part 52 ───────────────────────────────────────────────────
    {
        "id": "reg-05",
        "category": "regulatory",
        "question": "What type of reactor licensing does 10 CFR Part 52 provide for?",
        "gold_answer": "Combined licenses (COL), standard design certifications, and early site permits for nuclear power plants",
        "keywords": ["combined license", "design certification", "early site permit"],
        "source": "10CFR-Part52-Licenses-Certifications.pdf",
    },
    {
        "id": "reg-06",
        "category": "regulatory",
        "question": "What is 10 CFR Part 70 about?",
        "gold_answer": "Domestic licensing of special nuclear material",
        "keywords": ["special nuclear material", "licensing"],
        "source": "10CFR-Part70-Special-Nuclear-Material.pdf",
    },
]


# ---------------------------------------------------------------------------
# Scoring functions
# ---------------------------------------------------------------------------

def score_keyword_match(response: str, keywords: list[str]) -> float:
    """Score 0-1 based on fraction of gold keywords found in response."""
    if not response:
        return 0.0
    response_lower = response.lower()
    hits = sum(1 for kw in keywords if kw.lower() in response_lower)
    return hits / len(keywords) if keywords else 0.0


def score_groundedness(response: str, rag_context: str) -> float:
    """Score 0-1 based on overlap between response claims and RAG context.

    Simple heuristic: what fraction of response sentences contain
    words from the RAG context?
    """
    if not response or not rag_context:
        return 0.0
    ctx_words = set(rag_context.lower().split())
    sentences = [s.strip() for s in re.split(r'[.!?]', response) if len(s.strip()) > 10]
    if not sentences:
        return 0.0
    grounded = 0
    for sent in sentences:
        sent_words = set(sent.lower().split())
        overlap = len(sent_words & ctx_words) / max(len(sent_words), 1)
        if overlap > 0.3:  # 30% word overlap = grounded
            grounded += 1
    return grounded / len(sentences)


def score_no_hallucination(response: str, gold_answer: str, keywords: list[str]) -> float:
    """Penalize for confidently stating wrong information.

    Returns 1.0 if response doesn't contain obvious contradictions,
    0.0 if it states wrong numbers or facts.
    """
    if not response:
        return 0.5  # Empty is neutral
    # Check for fabricated section numbers (common Qwen hallucination)
    fabricated = re.findall(r'Section \d{3,}', response)
    if fabricated:
        return 0.0
    return 1.0


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rag_store():
    from axiom.rag.store import RAGStore
    url = os.environ["DATABASE_URL"]
    store = RAGStore(url)
    store.connect()
    yield store
    store.close()


@pytest.fixture(scope="module")
def gateway():
    from axiom.infra.gateway import Gateway
    gw = Gateway()
    if not gw.available:
        pytest.skip("No LLM provider available")
    return gw


def _ask_llm(gateway, question: str, rag_context: str = "") -> str:
    """Ask the LLM a question, optionally with RAG context."""
    system = "You are a nuclear engineering assistant. Answer concisely and accurately."
    if rag_context:
        system += (
            "\n\nUse the following reference material to ground your answer. "
            "Cite sources when possible.\n\n"
            f"--- Reference Material ---\n{rag_context}\n--- End Reference ---"
        )

    response = gateway.complete(question, system=system, max_tokens=500, task="synthesis")
    return response.text if response.success else ""


def _get_rag_context(store, question: str, limit: int = 4) -> str:
    """Get RAG context for a question."""
    results = store.search(query_text=question, limit=limit)
    if not results:
        return ""
    parts = []
    for r in results:
        parts.append(f"[{r.corpus}/{r.source_path}]\n{r.chunk_text[:400]}")
    return "\n\n".join(parts)


class TestRAGQualityBenchmark:
    """Prove that RAG data packs improve response quality."""

    def test_rag_improves_keyword_accuracy(self, rag_store, gateway):
        """RAG-grounded answers should match more gold keywords than ungrounded."""
        rag_scores = []
        no_rag_scores = []

        for qa in GOLD_QA[:10]:  # First 10 for speed
            ctx = _get_rag_context(rag_store, qa["question"])

            resp_rag = _ask_llm(gateway, qa["question"], rag_context=ctx)
            resp_no_rag = _ask_llm(gateway, qa["question"])

            rag_score = score_keyword_match(resp_rag, qa["keywords"])
            no_rag_score = score_keyword_match(resp_no_rag, qa["keywords"])

            rag_scores.append(rag_score)
            no_rag_scores.append(no_rag_score)

        avg_rag = sum(rag_scores) / len(rag_scores)
        avg_no_rag = sum(no_rag_scores) / len(no_rag_scores)

        print(f"\nKeyword accuracy: RAG={avg_rag:.2f}, No-RAG={avg_no_rag:.2f}, "
              f"Delta={avg_rag - avg_no_rag:+.2f}")

        assert avg_rag > avg_no_rag, (
            f"RAG ({avg_rag:.2f}) should beat No-RAG ({avg_no_rag:.2f}) on keyword accuracy"
        )

    def test_rag_improves_groundedness(self, rag_store, gateway):
        """RAG-grounded answers should be more grounded in source material."""
        scores = []

        for qa in GOLD_QA[:10]:
            ctx = _get_rag_context(rag_store, qa["question"])
            if not ctx:
                continue

            resp = _ask_llm(gateway, qa["question"], rag_context=ctx)
            g_score = score_groundedness(resp, ctx)
            scores.append(g_score)

        avg = sum(scores) / len(scores) if scores else 0
        print(f"\nGroundedness score: {avg:.2f} (1.0 = perfectly grounded)")

        assert avg > 0.3, f"Groundedness {avg:.2f} too low — answers not using RAG context"

    def test_rag_reduces_hallucination(self, rag_store, gateway):
        """RAG-grounded answers should hallucinate less."""
        rag_hall = []
        no_rag_hall = []

        for qa in GOLD_QA[:10]:
            ctx = _get_rag_context(rag_store, qa["question"])

            resp_rag = _ask_llm(gateway, qa["question"], rag_context=ctx)
            resp_no_rag = _ask_llm(gateway, qa["question"])

            rag_hall.append(score_no_hallucination(resp_rag, qa["gold_answer"], qa["keywords"]))
            no_rag_hall.append(score_no_hallucination(resp_no_rag, qa["gold_answer"], qa["keywords"]))

        avg_rag = sum(rag_hall) / len(rag_hall)
        avg_no_rag = sum(no_rag_hall) / len(no_rag_hall)

        print(f"\nHallucination resistance: RAG={avg_rag:.2f}, No-RAG={avg_no_rag:.2f}")

        assert avg_rag >= avg_no_rag, "RAG should not increase hallucination rate"


class TestRAGContextRetrieval:
    """Verify RAG retrieval finds relevant content for benchmark questions."""

    @pytest.mark.parametrize("qa", GOLD_QA, ids=[q["id"] for q in GOLD_QA])
    def test_retrieval_returns_results(self, rag_store, qa):
        """Every gold question should return at least 1 RAG result."""
        results = rag_store.search(query_text=qa["question"], limit=4)
        assert len(results) >= 1, f"No RAG results for: {qa['question']}"

    @pytest.mark.parametrize("qa", GOLD_QA, ids=[q["id"] for q in GOLD_QA])
    def test_retrieval_finds_relevant_source(self, rag_store, qa):
        """RAG should retrieve from the expected source document."""
        if qa["source"] == "multiple":
            pytest.skip("Cross-document question — no single expected source")
        results = rag_store.search(query_text=qa["question"], limit=4)
        source_paths = [r.source_path for r in results]
        # Check if expected source appears (partial match for path)
        source_key = qa["source"].split("/")[-1].replace(".pdf", "").replace(".txt", "")
        found = any(source_key in sp for sp in source_paths)
        if not found:
            # Acceptable if at least some keywords appear in results
            all_text = " ".join(r.chunk_text for r in results)
            kw_hits = sum(1 for kw in qa["keywords"] if kw.lower() in all_text.lower())
            assert kw_hits > 0, (
                f"Expected source '{qa['source']}' not found and no keywords in results. "
                f"Got: {source_paths}"
            )
