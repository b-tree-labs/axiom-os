# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LangfuseTraceProvider — HTTP-based; exercised via a fake transport.

The provider speaks the LangFuse public ingestion API directly (raw HTTPS POST
to ``/api/public/ingestion``), so tests inject a fake transport that captures
the batched events. No third-party SDK is involved.
"""

from __future__ import annotations

from typing import Any


class _FakeTransport:
    """Captures `post_batch` calls for inspection in tests."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self._fail_with = fail_with

    def post_batch(self, events: list[dict[str, Any]]) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.batches.append(list(events))


def test_langfuse_provider_satisfies_protocol() -> None:
    from axiom.infra.tracing import TraceProvider
    from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

    p = LangfuseTraceProvider(transport=_FakeTransport())
    assert isinstance(p, TraceProvider)


def test_langfuse_provider_buffers_events_and_flushes_one_batch() -> None:
    from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

    transport = _FakeTransport()
    p = LangfuseTraceProvider(transport=transport)

    tid = p.start_trace("research", session="s1")
    p.log_generation(tid, model="bonsai", prompt="q", output="a", tokens=42)
    p.log_retrieval(tid, query="q", results=[{"id": 1}], k=1)
    p.score(tid, name="faithfulness", value=0.9)

    # Nothing should hit the wire until flush().
    assert transport.batches == []

    p.flush()

    assert len(transport.batches) == 1
    events = transport.batches[0]
    assert len(events) == 4

    trace_evt, gen_evt, span_evt, score_evt = events

    assert trace_evt["type"] == "trace-create"
    assert trace_evt["body"]["id"] == tid
    assert trace_evt["body"]["name"] == "research"
    assert trace_evt["body"]["metadata"] == {"session": "s1"}

    assert gen_evt["type"] == "generation-create"
    assert gen_evt["body"]["traceId"] == tid
    assert gen_evt["body"]["model"] == "bonsai"
    assert gen_evt["body"]["input"] == "q"
    assert gen_evt["body"]["output"] == "a"
    assert gen_evt["body"]["metadata"] == {"tokens": 42}

    assert span_evt["type"] == "span-create"
    assert span_evt["body"]["traceId"] == tid
    assert span_evt["body"]["name"] == "retrieval"
    assert span_evt["body"]["input"] == {"query": "q"}
    assert span_evt["body"]["output"] == {"results": [{"id": 1}]}
    assert span_evt["body"]["metadata"] == {"k": 1}

    assert score_evt["type"] == "score-create"
    assert score_evt["body"]["traceId"] == tid
    assert score_evt["body"]["name"] == "faithfulness"
    assert score_evt["body"]["value"] == 0.9


def test_langfuse_provider_each_event_has_unique_envelope_id_and_iso_timestamp() -> None:
    from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

    transport = _FakeTransport()
    p = LangfuseTraceProvider(transport=transport)
    tid = p.start_trace("t")
    p.log_generation(tid, model="m", prompt="p", output="o")
    p.flush()

    events = transport.batches[0]
    envelope_ids = {e["id"] for e in events}
    assert len(envelope_ids) == len(events)  # all unique
    for e in events:
        assert "T" in e["timestamp"] and e["timestamp"].endswith("+00:00")


def test_langfuse_provider_unknown_trace_id_is_safe() -> None:
    from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

    transport = _FakeTransport()
    p = LangfuseTraceProvider(transport=transport)
    # Logging against an unknown trace id should not raise — degrade gracefully.
    p.log_generation("bogus", model="m", prompt="p", output="o")
    p.log_retrieval("bogus", query="q", results=[])
    p.score("bogus", name="s", value=1.0)
    p.flush()
    # No real events; if a batch was sent at all it must be empty.
    for b in transport.batches:
        assert b == []


def test_langfuse_provider_flush_clears_buffer() -> None:
    from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

    transport = _FakeTransport()
    p = LangfuseTraceProvider(transport=transport)
    tid = p.start_trace("t")
    p.log_generation(tid, model="m", prompt="p", output="o")
    p.flush()
    p.flush()  # second flush should not resend the first batch
    # Either one batch or one batch + an empty one — never two with content.
    nonempty = [b for b in transport.batches if b]
    assert len(nonempty) == 1


def test_langfuse_provider_transport_failure_does_not_crash_host() -> None:
    from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

    transport = _FakeTransport(fail_with=RuntimeError("network down"))
    p = LangfuseTraceProvider(transport=transport)
    tid = p.start_trace("t")
    p.log_generation(tid, model="m", prompt="p", output="o")
    # A buggy network must not bring a user request down.
    p.flush()


def test_langfuse_provider_default_transport_built_from_keys_and_host() -> None:
    """Without an injected transport, the provider builds an HTTP one from creds."""
    from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

    p = LangfuseTraceProvider(
        public_key="pk-x", secret_key="sk-x", host="http://lf.local"
    )
    # Implementation detail: the default transport stores its target host;
    # we don't fire it (no network), just confirm it built without error.
    assert p is not None
