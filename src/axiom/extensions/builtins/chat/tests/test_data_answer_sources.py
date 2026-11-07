# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A data answer cites the data it read, and a document only when it used one.

A bound chat answered "what's the peak in the focused chart?" from a ``data.show`` read, and the
answer's Sources listed four reports the retriever had surfaced and the answer never used. The
retriever runs on every question; what it surfaced is not what grounded a number read from data.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from axiom.extensions.builtins.chat.answer_sources import (
    DATA_CORPUS,
    data_read_sources,
    sources_for_answer,
)
from axiom.extensions.builtins.chat.providers.base import citation_entries
from axiom.rag.retriever import RetrievedChunk
from axiom.rag.titles import clean_title

_READ = {
    "tool": "data.show",
    "args": {"site": "site-a", "feed": "instrument", "channels": ["Power"],
             "start": "2026-10-05T00:00:00Z", "end": "2026-10-06T00:00:00Z"},
}


def _chunk(key: str, rank: int, title: str, path: str) -> RetrievedChunk:
    return RetrievedChunk(
        citation_key=key, rank=rank, source_path=path, source_title=title,
        chunk_text="body", chunk_index=0, corpus="rag-org", similarity=0.8, rrf_score=0.5,
    )


_DOCS = [_chunk("C1", 1, "Report one", "/r1.pdf"), _chunk("C2", 2, "Report two", "/r2.pdf")]


class TestADataOnlyAnswer:
    def test_lists_the_read_and_no_documents(self):
        got = sources_for_answer("The peak was 15.2 W at 09:34.", _DOCS, reads=[_READ])
        assert [s.citation_key for s in got] == ["D1"]
        assert got[0].corpus == DATA_CORPUS

    def test_the_read_names_site_feed_channels_window_and_verb(self):
        [src] = data_read_sources([_READ], [])
        for part in ("data.show", "site=site-a", "feed=instrument", "channels=Power",
                     "start=2026-10-05T00:00:00Z", "end=2026-10-06T00:00:00Z"):
            assert part in src.source_title

    def test_a_verb_the_node_ran_is_a_read_too(self):
        got = sources_for_answer("It read 3.", _DOCS, served=["data.aggregate"])
        assert [s.citation_key for s in got] == ["D1"]
        assert "data.aggregate" in got[0].source_title

    def test_identical_reads_are_listed_once(self):
        assert len(data_read_sources([_READ, _READ], [])) == 1


class TestAMixedAnswer:
    def test_lists_the_reads_and_only_the_cited_documents(self):
        got = sources_for_answer("Peak 15.2 W; the license limit is 1.1 MW [C2].", _DOCS, reads=[_READ])
        assert [s.citation_key for s in got] == ["D1", "C2"]

    def test_the_reads_lead_the_rendered_list(self):
        got = sources_for_answer("Limit [C1].", _DOCS, reads=[_READ])
        assert [e["key"] for e in citation_entries(got)] == ["D1", "C1"]


class TestADocumentAnswerIsUnchanged:
    def test_no_read_means_the_retrieval_is_the_sources(self):
        assert sources_for_answer("An answer.", _DOCS) == _DOCS

    def test_no_read_and_no_retrieval_is_nothing(self):
        assert sources_for_answer("An answer.", []) == []


class TestTheAgentEmitsWhatTheAnswerUsed:
    def _agent(self, answer: str, turn_data: dict):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.extensions.builtins.chat.scope import ChatScope

        a = ChatAgent.__new__(ChatAgent)
        a._render = MagicMock()
        s = ChatScope.__new__(ChatScope)
        s.last_retrieved = list(_DOCS)
        s.session = SimpleNamespace(
            context={"turn_data": turn_data},
            messages=[SimpleNamespace(role="assistant", content=answer, timestamp="t1")],
        )
        return a, s

    def test_a_data_turn_emits_the_read_and_no_uncited_documents(self):
        a, s = self._agent("Peak 15.2 W.", {"data_reads": [_READ]})
        a._emit_citations(scope=s)
        (passed,), _ = a._render.render_citations.call_args
        assert [c.citation_key for c in passed] == ["D1"]

    def test_a_data_tool_call_is_recorded_as_a_read(self):
        from axiom.extensions.builtins.chat.agent import _note_data_read

        _, s = self._agent("", {"data_tools": ["data.show"]})
        _note_data_read(s, "data.show", {"site": "site-a"})
        _note_data_read(s, "read_file", {"path": "/x"})
        assert s.session.context["turn_data"]["data_reads"] == [
            {"tool": "data.show", "args": {"site": "site-a"}}
        ]


class TestAGarbledTitle:
    _RAW = "04/21/2015DDDDDDDDDDD      STATUS WINDOW  - 2.38    DDDDDDDDDDDDDDDDD11:02"

    def test_fill_runs_and_padding_are_removed(self):
        assert clean_title(self._RAW, "04-21-2015 Status.txt") == "04/21/2015 STATUS WINDOW - 2.38 11:02"

    def test_a_title_that_is_only_filler_falls_back_to_the_file_name(self):
        assert clean_title("DDDDDDDD ======", "/docs/04-21-2015 Status.txt") == "04-21-2015 Status.txt"

    def test_an_ordinary_title_is_untouched(self):
        assert clean_title("Rod worth (2019 rev. A)", "/x.md") == "Rod worth (2019 rev. A)"

    def test_an_empty_title_stays_empty(self):
        assert clean_title("", "/x.md") == ""

    def test_the_rendered_citation_shows_the_clean_title(self):
        [entry] = citation_entries([_chunk("C1", 1, self._RAW, "04-21-2015 Status.txt")])
        assert entry["title"] == "04/21/2015 STATUS WINDOW - 2.38 11:02"
