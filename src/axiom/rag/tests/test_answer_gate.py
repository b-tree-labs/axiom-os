# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the answer-provenance standing.

The verification underneath is already covered by
``test_citation_postprocessor``. What is under test here is the decision: an
answer that claims evidence which does not exist has to be distinguishable
from one that claims none, and both from one whose claims hold up.
"""

from __future__ import annotations

import pytest

from axiom.rag.answer_gate import (
    ProvenanceFinding,
    ProvenanceStanding,
    check_answer_provenance,
)
from axiom.rag.retriever import RetrievedChunk


def chunk(key: str, path: str = "handbook.md") -> RetrievedChunk:
    return RetrievedChunk(
        citation_key=key,
        rank=int(key[1:]),
        source_path=path,
        source_title=path,
        chunk_text="...",
        chunk_index=int(key[1:]),
        corpus="rag-internal",
        similarity=0.5,
        rrf_score=0.03,
    )


class TestTheThreeStandings:
    def test_every_marker_resolving_is_grounded(self) -> None:
        finding = check_answer_provenance(
            "Coolant rose [C1] and then settled [C2].", [chunk("C1"), chunk("C2")]
        )
        assert finding.standing is ProvenanceStanding.GROUNDED
        assert finding.cited == ("C1", "C2")
        assert finding.unresolved == ()

    def test_a_marker_naming_a_chunk_nobody_retrieved_is_ungrounded(self) -> None:
        finding = check_answer_provenance(
            "Coolant rose [C1], per the procedure [C7].", [chunk("C1")]
        )
        assert finding.standing is ProvenanceStanding.UNGROUNDED
        assert finding.unresolved == ("C7",)
        assert "C7" in finding.reason

    def test_no_markers_at_all_is_uncited_not_grounded(self) -> None:
        # A greeting is uncited and fine. It is its own standing because
        # whether that is acceptable depends on the surface, and folding it
        # into GROUNDED would let an ungrounded assertion pass as a checked one.
        finding = check_answer_provenance("Good morning.", [])
        assert finding.standing is ProvenanceStanding.UNCITED
        assert finding.cited == ()


class TestTheCaseThatWasNeverExamined:
    """Retrieval returning nothing must not skip the check.

    The call site this replaces returned early when nothing was retrieved, so
    an answer that invented its citations outright — the worst version of the
    problem — was the one case never looked at.
    """

    def test_citations_with_no_retrieval_at_all_are_ungrounded(self) -> None:
        finding = check_answer_provenance("The limit is 3.2 [C1].", [])
        assert finding.standing is ProvenanceStanding.UNGROUNDED
        assert finding.unresolved == ("C1",)
        assert finding.retrieved_count == 0

    def test_the_reason_says_nothing_was_retrieved(self) -> None:
        finding = check_answer_provenance("The limit is 3.2 [C1].", [])
        assert "nothing was retrieved" in finding.reason

    def test_a_none_retrieval_is_the_same_as_an_empty_one(self) -> None:
        finding = check_answer_provenance("The limit is 3.2 [C1].", None)
        assert finding.standing is ProvenanceStanding.UNGROUNDED

    def test_an_uncited_answer_with_no_retrieval_is_still_only_uncited(self) -> None:
        finding = check_answer_provenance("I don't know.", None)
        assert finding.standing is ProvenanceStanding.UNCITED


class TestWhatTheFindingCarries:
    def test_retrieved_but_never_cited_chunks_are_reported(self) -> None:
        finding = check_answer_provenance("Coolant rose [C1].", [chunk("C1"), chunk("C2")])
        assert finding.unused == ("C2",)
        assert finding.retrieved_count == 2

    def test_an_answer_ignoring_everything_retrieved_is_uncited(self) -> None:
        finding = check_answer_provenance("Coolant rose.", [chunk("C1")])
        assert finding.standing is ProvenanceStanding.UNCITED
        assert finding.unused == ("C1",)
        assert "none was used" in finding.reason

    def test_a_grounded_reason_names_the_citations(self) -> None:
        finding = check_answer_provenance("Rose [C1].", [chunk("C1")])
        assert "C1" in finding.reason

    def test_unresolved_wins_over_uncited_when_both_could_apply(self) -> None:
        # Every marker unresolved means `cited` is empty too. That must read
        # as UNGROUNDED, not as "the answer cited nothing" — the answer very
        # much cited something; it cited something that is not there.
        finding = check_answer_provenance("Per [C4].", [chunk("C1")])
        assert finding.standing is ProvenanceStanding.UNGROUNDED
        assert finding.cited == ()


class TestTheFindingIsNotABoolean:
    @pytest.mark.parametrize(
        "answer,retrieved,expected",
        [
            ("Rose [C1].", [chunk("C1")], ProvenanceStanding.GROUNDED),
            ("Rose [C9].", [chunk("C1")], ProvenanceStanding.UNGROUNDED),
            ("Rose.", [], ProvenanceStanding.UNCITED),
        ],
    )
    def test_truth_testing_raises_in_every_standing(self, answer, retrieved, expected) -> None:
        finding = check_answer_provenance(answer, retrieved)
        assert finding.standing is expected
        with pytest.raises(TypeError) as excinfo:
            bool(finding)
        assert "standing" in str(excinfo.value)

    def test_a_finding_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        finding = check_answer_provenance("Rose [C1].", [chunk("C1")])
        with pytest.raises(FrozenInstanceError):
            finding.standing = ProvenanceStanding.UNCITED  # type: ignore[misc]

    def test_the_finding_type_is_what_is_returned(self) -> None:
        assert isinstance(check_answer_provenance("x", []), ProvenanceFinding)
