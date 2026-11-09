# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for post-filter policy-breach detection (#40).

Per Collaborative Memory §6 admission: LLMs leak even under policy
enforcement at retrieval time. Need a deterministic post-check on
generated output against the visible-fragment set before emitting.

Detects two signals:
1. Direct fragment-ID citation (uuid pattern) for a non-visible id.
2. Exact content quotes (≥ min_quote_words) from a non-visible
   fragment's content.

Does NOT attempt semantic paraphrase detection — that requires
another LLM. Deterministic gate only; complementary to the LLM's
own safety layer.
"""

from __future__ import annotations


def _make_fragment(id_hint: str, fact: str):
    """Build a fragment with a known id prefix for testable assertions."""
    import dataclasses

    from axiom.memory.fragment import create_fragment

    f = create_fragment(
        content={"fact": fact}, cognitive_type="semantic",
        principal_id="u1", agents=set(), resources=set(),
    )
    # Replace id with a deterministic uuid-shaped string for the test
    fake_id = f"{id_hint}{'0' * (36 - len(id_hint))}"
    return dataclasses.replace(f, id=fake_id)


class TestCleanOutput:
    def test_plain_text_no_breach(self):
        from axiom.memory.post_filter import check_llm_output

        visible = _make_fragment("11111111-1111-1111-1111-11111111", "fission basics")
        result = check_llm_output(
            output="Fission is the process of splitting heavy nuclei.",
            visible_fragments=[visible],
            all_fragments=[visible],
        )
        assert result.is_clean is True
        assert result.breaches == []


class TestIdCitationBreach:
    def test_cited_non_visible_id_flagged(self):
        from axiom.memory.post_filter import check_llm_output

        visible = _make_fragment("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa", "allowed fact")
        secret = _make_fragment("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb", "secret fact")

        output = f"According to fragment {secret.id}, the key is xyz."
        result = check_llm_output(
            output=output,
            visible_fragments=[visible],
            all_fragments=[visible, secret],
        )
        assert result.is_clean is False
        assert len(result.breaches) == 1
        assert result.breaches[0]["fragment_id"] == secret.id
        assert result.breaches[0]["reason"] == "id_citation"

    def test_visible_id_citation_not_flagged(self):
        from axiom.memory.post_filter import check_llm_output

        visible = _make_fragment("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa", "open fact")
        output = f"See fragment {visible.id} for details."
        result = check_llm_output(
            output=output,
            visible_fragments=[visible],
            all_fragments=[visible],
        )
        assert result.is_clean is True


class TestQuoteBreach:
    def test_long_quote_from_non_visible_flagged(self):
        from axiom.memory.post_filter import check_llm_output

        visible = _make_fragment("11111111-1111-1111-1111-11111111", "public material")
        secret = _make_fragment(
            "22222222-2222-2222-2222-22222222",
            "The classified launch code is golf tango seven seven alpha bravo",
        )
        # LLM quoted the classified content verbatim
        output = "The classified launch code is golf tango seven seven alpha bravo — or so the rumor goes."
        result = check_llm_output(
            output=output,
            visible_fragments=[visible],
            all_fragments=[visible, secret],
            min_quote_words=5,
        )
        assert result.is_clean is False
        assert any(b["reason"] == "content_quote" for b in result.breaches)

    def test_short_quote_not_flagged(self):
        """Short quotes might be coincidental."""
        from axiom.memory.post_filter import check_llm_output

        visible = _make_fragment("11111111-1111-1111-1111-11111111", "public")
        secret = _make_fragment(
            "22222222-2222-2222-2222-22222222",
            "the sky is blue",
        )
        output = "We can all agree that the sky is blue, which is common knowledge."
        result = check_llm_output(
            output=output,
            visible_fragments=[visible],
            all_fragments=[visible, secret],
            min_quote_words=10,  # requires ≥10 word run match
        )
        assert result.is_clean is True


class TestRedaction:
    def test_redact_id_citations(self):
        from axiom.memory.post_filter import redact_breaches

        visible = _make_fragment("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa", "open")
        secret = _make_fragment("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb", "secret")

        output = f"See {secret.id} for info."
        redacted = redact_breaches(
            output,
            visible_fragments=[visible],
            all_fragments=[visible, secret],
        )
        assert secret.id not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_content_quote(self):
        from axiom.memory.post_filter import redact_breaches

        visible = _make_fragment("11111111-1111-1111-1111-11111111", "x")
        secret = _make_fragment(
            "22222222-2222-2222-2222-22222222",
            "top secret password is hunter2",
        )
        output = "The answer is: top secret password is hunter2 — simple."
        redacted = redact_breaches(
            output,
            visible_fragments=[visible],
            all_fragments=[visible, secret],
            min_quote_words=5,
        )
        assert "hunter2" not in redacted
        assert "[REDACTED]" in redacted


class TestResultShape:
    def test_result_dataclass_fields(self):
        from axiom.memory.post_filter import check_llm_output

        visible = _make_fragment("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa", "x")
        result = check_llm_output(
            output="nothing suspicious",
            visible_fragments=[visible],
            all_fragments=[visible],
        )
        # Result carries both verdict + breach list + the checked output
        assert hasattr(result, "is_clean")
        assert hasattr(result, "breaches")
        assert hasattr(result, "output")
