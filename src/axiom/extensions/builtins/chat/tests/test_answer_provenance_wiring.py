# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The provenance check is actually called, on every completed turn.

The module it calls is tested on its own. What these pin is the wiring,
because an unwired check is the exact failure this work exists to fix: the
verification it replaces ran, produced the right answer, and wrote it to a
log row nobody read.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.rag.answer_gate import ProvenanceStanding
from axiom.rag.retriever import RetrievedChunk


def chunk(key: str) -> RetrievedChunk:
    return RetrievedChunk(
        citation_key=key,
        rank=int(key[1:]),
        source_path="handbook.md",
        source_title="handbook.md",
        chunk_text="...",
        chunk_index=int(key[1:]),
        corpus="rag-internal",
        similarity=0.5,
        rrf_score=0.03,
    )


@pytest.fixture
def agent() -> ChatAgent:
    return ChatAgent.__new__(ChatAgent)


@pytest.fixture
def scope() -> ChatScope:
    return ChatScope.__new__(ChatScope)


def _scope_with(retrieved) -> ChatScope:
    s = ChatScope.__new__(ChatScope)
    s.last_retrieved = retrieved
    s.last_provenance = None
    return s


class TestTheCheckRuns:
    def test_a_resolved_citation_records_grounded(self, agent) -> None:
        s = _scope_with([chunk("C1")])
        agent._check_answer_provenance("Rose [C1].", scope=s)
        assert s.last_provenance.standing is ProvenanceStanding.GROUNDED

    def test_an_unresolved_citation_records_ungrounded(self, agent) -> None:
        s = _scope_with([chunk("C1")])
        agent._check_answer_provenance("Rose [C1] and [C9].", scope=s)
        assert s.last_provenance.standing is ProvenanceStanding.UNGROUNDED
        assert s.last_provenance.unresolved == ("C9",)

    def test_the_empty_retrieval_turn_is_examined_not_skipped(self, agent) -> None:
        # The whole point. The audit path this sits beside returns early when
        # nothing was retrieved, so the answer that invented its citations
        # outright was the one case never looked at.
        s = _scope_with([])
        agent._check_answer_provenance("The limit is 3.2 [C1].", scope=s)
        assert s.last_provenance is not None, "an empty retrieval must still be checked"
        assert s.last_provenance.standing is ProvenanceStanding.UNGROUNDED

    def test_an_ordinary_uncited_answer_records_uncited(self, agent) -> None:
        s = _scope_with([])
        agent._check_answer_provenance("Good morning.", scope=s)
        assert s.last_provenance.standing is ProvenanceStanding.UNCITED


class TestFailureIsVisibleAsAbsence:
    def test_a_broken_check_leaves_the_finding_absent_not_grounded(self, agent) -> None:
        # The method swallows its own errors so a check failure never blocks
        # an answer. That is only safe because absence is distinguishable
        # from a passing finding: None is not GROUNDED, and a caller that
        # treats it as one has made a decision rather than inherited a default.
        s = _scope_with(object())  # not iterable — the check will raise
        agent._check_answer_provenance("Rose [C1].", scope=s)
        assert s.last_provenance is None

    def test_a_broken_check_does_not_propagate(self, agent) -> None:
        s = _scope_with(object())
        agent._check_answer_provenance("Rose [C1].", scope=s)  # must not raise


class TestItIsCalledFromTheTurn:
    def test_the_finalize_path_calls_it_before_returning(self) -> None:
        # Guards the call site itself. A refactor that drops the call would
        # otherwise leave every unit test above passing.
        import inspect

        source = inspect.getsource(ChatAgent._turn_impl)
        assert "_check_answer_provenance" in source, (
            "the completed-turn path must call the provenance check; without "
            "the call, the module is verification nobody runs"
        )
