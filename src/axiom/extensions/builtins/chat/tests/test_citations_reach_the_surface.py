# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A grounded answer says what grounded it, on every surface.

The retrieval stack already computes provenance for every answer: `retrieve()`
returns `RetrievedChunk`s carrying `citation_key` ("C1", "C2", ...), rank,
source path, title and corpus, and the agent already parks them on
`scope.last_retrieved`. Nothing consumed them. The renderer protocol had no
method for citations, so no surface could show where an answer came from, and
the web surface could not have shown them even if it wanted to because nothing
was on the wire.

These tests pin the whole chain: the protocol carries it, every provider
implements it, the web provider puts it on the wire in a shape a browser can
render, and an answer with no retrieval says nothing rather than saying
"sources: none".
"""

from __future__ import annotations

from axiom.rag.retriever import RetrievedChunk


def _chunk(key: str, rank: int, title: str, path: str) -> RetrievedChunk:
    return RetrievedChunk(
        citation_key=key, rank=rank, source_path=path, source_title=title,
        chunk_text="body", chunk_index=0, corpus="rag-org",
        similarity=0.8, rrf_score=0.5,
    )


class TestTheProtocolCarriesCitations:
    def test_the_base_provider_declares_render_citations(self):
        from axiom.extensions.builtins.chat.providers.base import RenderProvider

        assert hasattr(RenderProvider, "render_citations")

    def test_every_shipped_provider_implements_it(self):
        """The web provider stubbing what the terminal renders is the gap this
        whole review found; a new protocol method must not repeat it."""
        from axiom.extensions.builtins.chat import fullscreen
        from axiom.extensions.builtins.chat.providers import (
            ansi_render,
            null_render,
            rich_render,
            sse_render,
        )
        from axiom.extensions.builtins.chat.providers.base import RenderProvider

        # fullscreen holds a FIFTH provider (_TuiRenderProvider). An earlier
        # version of this test listed only the four under providers/ and would
        # have let the TUI silently drop citations.
        for mod in (ansi_render, null_render, rich_render, sse_render, fullscreen):
            # Each module imports the BASE class into its namespace, so matching
            # on the name alone picks up the base and passes for free. Require a
            # real subclass, and require the method in its OWN __dict__ so an
            # inherited abstract stub does not count.
            cls = next(
                v for v in vars(mod).values()
                if isinstance(v, type)
                and issubclass(v, RenderProvider)
                and v is not RenderProvider
            )
            assert "render_citations" in vars(cls), f"{cls.__name__} does not implement it"


class TestTheWebProviderPutsThemOnTheWire:
    def _emitted(self, chunks):
        from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider

        frames: list[dict] = []
        SseRenderProvider(frames.append).render_citations(chunks)
        return frames

    def test_a_citations_frame_is_emitted(self):
        frames = self._emitted([_chunk("C1", 1, "Rod worth", "/ops/rod.md")])
        assert len(frames) == 1
        assert "citations" in frames[0]

    def test_the_frame_carries_what_a_browser_needs_to_render_a_source(self):
        [frame] = self._emitted([_chunk("C1", 1, "Rod worth", "/ops/rod.md")])
        [cite] = frame["citations"]
        assert cite["key"] == "C1"
        assert cite["title"] == "Rod worth"
        assert cite["path"] == "/ops/rod.md"
        assert cite["rank"] == 1

    def test_order_is_the_retrieval_rank(self):
        [frame] = self._emitted([
            _chunk("C2", 2, "second", "/b.md"),
            _chunk("C1", 1, "first", "/a.md"),
        ])
        assert [c["key"] for c in frame["citations"]] == ["C1", "C2"]

    def test_one_frame_per_source_path_not_per_chunk(self):
        """Three chunks of one document are one source, not three."""
        [frame] = self._emitted([
            _chunk("C1", 1, "Rod worth", "/ops/rod.md"),
            _chunk("C2", 2, "Rod worth", "/ops/rod.md"),
            _chunk("C3", 3, "Other", "/ops/other.md"),
        ])
        assert [c["path"] for c in frame["citations"]] == ["/ops/rod.md", "/ops/other.md"]

    def test_an_ungrounded_answer_emits_nothing(self):
        """Silence, not an empty sources list that renders as a bare heading."""
        assert self._emitted([]) == []


class TestTheFrameIsSerialisable:
    def test_the_frame_survives_json(self):
        import json

        from axiom.extensions.builtins.chat.providers.sse_render import SseRenderProvider

        frames: list[dict] = []
        SseRenderProvider(frames.append).render_citations([_chunk("C1", 1, "t", "/p.md")])
        assert json.loads(json.dumps(frames[0]))["citations"][0]["key"] == "C1"


class TestTheAgentActuallyEmitsThem:
    """A renderer method nobody calls is an unbuilt mechanism.

    The retrieval stack has computed `citation_key` for a long time and the
    agent has parked the chunks on `scope.last_retrieved` for a long time.
    What was missing was the call, which is the same failure the provenance
    check was written to avoid: a verification that ran and wrote to a row
    nobody read.
    """

    def _agent_with_render(self):
        from unittest.mock import MagicMock

        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.extensions.builtins.chat.scope import ChatScope

        a = ChatAgent.__new__(ChatAgent)
        a._render = MagicMock()
        s = ChatScope.__new__(ChatScope)
        s.last_retrieved = [_chunk("C1", 1, "Rod worth", "/ops/rod.md")]
        return a, s

    def test_the_turn_hands_the_chunks_to_the_renderer(self):
        a, s = self._agent_with_render()
        a._emit_citations(scope=s)
        a._render.render_citations.assert_called_once()
        (passed,), _ = a._render.render_citations.call_args
        assert [c.citation_key for c in passed] == ["C1"]

    def test_an_ungrounded_turn_does_not_call_the_renderer(self):
        a, s = self._agent_with_render()
        s.last_retrieved = []
        a._emit_citations(scope=s)
        a._render.render_citations.assert_not_called()

    def test_a_failing_renderer_never_breaks_the_answer(self):
        a, s = self._agent_with_render()
        a._render.render_citations.side_effect = RuntimeError("terminal gone")
        a._emit_citations(scope=s)  # must not raise

    def test_the_finalize_path_calls_it_before_returning(self):
        """Guards the call site. A refactor that drops it would leave every
        unit test above passing while no surface ever showed a source."""
        import inspect

        from axiom.extensions.builtins.chat.agent import ChatAgent

        assert "_emit_citations" in inspect.getsource(ChatAgent._turn_impl), (
            "the completed-turn path must emit citations; without the call the "
            "provenance the retriever computed reaches no surface"
        )
