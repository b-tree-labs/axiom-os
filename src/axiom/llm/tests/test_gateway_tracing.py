# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every LLM call is traceable, from the one place they all pass through.

Tracing existed as infrastructure — a provider interface, a Langfuse client, a
factory, env-driven selection — and was wired into the research loop, the eval
harness and classroom. It was NOT wired into the gateway, so no ordinary chat
turn produced a trace, and a restored Langfuse would have shown nothing from
the surface that matters.

The gateway is the right seam because it is the only place every provider call
passes through, streaming and non-streaming alike. Two rules the tests below
exist to hold: tracing is free when unconfigured, and it can never cost a
caller their answer.
"""

from __future__ import annotations

from axiom.infra.tracing.in_memory_provider import InMemoryTraceProvider
from axiom.infra.tracing.null_provider import NullTraceProvider
from axiom.llm.gateway import Gateway


class TestItIsFreeUnlessConfigured:
    def test_the_default_tracer_is_the_null_one(self, monkeypatch):
        """No config, no cost. A node that never sets LANGFUSE_* pays nothing.

        The env is cleared explicitly: a developer machine that HAS the keys
        set would otherwise select Langfuse here and the test would assert the
        opposite of what it means to.
        """
        for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
                  "AXIOM_TRACE_BACKEND"):
            monkeypatch.delenv(k, raising=False)
        g = Gateway.__new__(Gateway)
        assert isinstance(g._tracer, NullTraceProvider)

    def test_keys_in_the_environment_select_langfuse(self, monkeypatch):
        """The other half: the wiring is env-driven, so a configured node gets
        the real backend without a code change."""
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        monkeypatch.delenv("AXIOM_TRACE_BACKEND", raising=False)
        g = Gateway.__new__(Gateway)
        assert type(g._tracer).__name__ == "LangfuseTraceProvider"

    def test_a_supplied_tracer_is_used(self):
        mem = InMemoryTraceProvider()
        g = Gateway.__new__(Gateway)
        g.set_tracer(mem)
        assert g._tracer is mem


class TestACompletionIsTraced:
    def _gw(self):
        mem = InMemoryTraceProvider()
        g = Gateway.__new__(Gateway)
        g.set_tracer(mem)
        return g, mem

    def test_a_trace_is_opened_for_the_call(self):
        g, mem = self._gw()
        tid = g._trace_call("extraction", tier_hint="smart", prompt="hi")
        assert tid
        assert mem.get_trace(tid) is not None

    def test_the_trace_names_the_task_and_tier(self):
        g, mem = self._gw()
        tid = g._trace_call("extraction", tier_hint="smart", prompt="hi")
        t = mem.get_trace(tid)
        blob = repr(t)
        assert "extraction" in blob
        assert "smart" in blob

    def test_the_generation_records_model_and_output(self):
        g, mem = self._gw()
        tid = g._trace_call("extraction", tier_hint=None, prompt="the question")
        g._trace_generation(tid, model="m-1", prompt="the question", output="the answer",
                            input_tokens=11, output_tokens=22, provider="anthropic")
        [gen] = [x for x in mem.generations if x["trace_id"] == tid]
        assert gen["model"] == "m-1"
        assert gen["output"] == "the answer"
        assert gen["metadata"]["input_tokens"] == 11
        assert gen["metadata"]["output_tokens"] == 22
        assert gen["metadata"]["provider"] == "anthropic"


class TestTracingNeverCostsTheCaller:
    """The rule that matters most: observability is not worth an outage."""

    class _Exploding:
        def start_trace(self, name, **kw):
            raise RuntimeError("langfuse down")

        def log_generation(self, *a, **kw):
            raise RuntimeError("langfuse down")

        def score(self, *a, **kw):
            raise RuntimeError("langfuse down")

        def flush(self):
            raise RuntimeError("langfuse down")

    def test_a_failing_start_trace_returns_no_id_and_does_not_raise(self):
        g = Gateway.__new__(Gateway)
        g.set_tracer(self._Exploding())
        assert g._trace_call("extraction", tier_hint=None, prompt="hi") == ""

    def test_a_failing_generation_does_not_raise(self):
        g = Gateway.__new__(Gateway)
        g.set_tracer(self._Exploding())
        g._trace_generation("whatever", model="m", prompt="p", output="o",
                            input_tokens=1, output_tokens=2)

    def test_logging_against_an_empty_trace_id_is_a_no_op(self):
        """start_trace failing must not make the generation call explode."""
        mem = InMemoryTraceProvider()
        g = Gateway.__new__(Gateway)
        g.set_tracer(mem)
        g._trace_generation("", model="m", prompt="p", output="o",
                            input_tokens=1, output_tokens=2)


class TestBothCallPathsAreActuallyWired:
    """A helper nobody calls is an unbuilt mechanism.

    Guards the call sites. Every unit test above would still pass if a refactor
    dropped the calls, and no surface would emit a trace.
    """

    def test_the_completion_path_opens_and_closes_a_trace(self):
        import inspect

        src = inspect.getsource(Gateway.complete)
        assert "_trace_call" in src
        assert "_trace_generation" in src

    def test_the_streaming_path_opens_and_closes_a_trace(self):
        """Streaming is the path chat uses, so this is the one that matters."""
        import inspect

        src = inspect.getsource(Gateway.stream_with_tools)
        assert "_trace_call" in src
        assert "_trace_generation" in src

    def test_the_streaming_path_records_usage_from_the_usage_chunk(self):
        """Token counts only exist once the stream ends; the wrapper must read
        them off StreamChunk(type="usage") rather than guessing."""
        import inspect

        src = inspect.getsource(Gateway.stream_with_tools)
        assert 'type == "usage"' in src
        assert "input_tokens" in src


class TestItDoesNotDependOnAnyOneChatSurface:
    """The gateway is the seam precisely because every Axiom surface uses it.

    The node's current OpenWebUI front end calls LiteLLM over its own httpx
    client and never constructs a Gateway, so it is not traced here — and when
    it is replaced, nothing about this wiring changes. Tracing follows the
    platform, not whichever UI is in front of it this quarter.
    """

    def test_tracing_lives_on_the_gateway_not_on_a_chat_module(self):
        import inspect

        from axiom.llm import gateway as gw

        src = inspect.getsource(gw)
        assert "_trace_call" in src

    def test_no_chat_or_webui_import_leaked_into_the_gateway(self):
        import inspect

        from axiom.llm import gateway as gw

        src = inspect.getsource(gw)
        for forbidden in ("open_webui", "openwebui", "fused_rag_shim"):
            assert forbidden not in src.lower(), f"gateway must not know about {forbidden}"
