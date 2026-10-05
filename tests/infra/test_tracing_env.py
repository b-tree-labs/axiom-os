# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for env-var driven tracing configuration — T1a.

For a deployment, the LangFuse provider must load automatically
from environment variables so a node operator never edits code to
enable observability. The env-var layer is deliberately the *only*
auto-configuration surface — explicit config dicts still work for
tests and advanced wiring.
"""

from __future__ import annotations

import pytest

from axiom.infra.tracing.env import (
    LANGFUSE_ENV_VARS,
    build_trace_provider_from_env,
    load_trace_config_from_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in LANGFUSE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("AXIOM_TRACE_BACKEND", raising=False)
    yield


# ---------------------------------------------------------------------------
# load_trace_config_from_env
# ---------------------------------------------------------------------------


class TestLoadConfigFromEnv:
    def test_empty_env_returns_null_backend(self):
        cfg = load_trace_config_from_env()
        assert cfg["backend"] == "null"

    def test_langfuse_keys_enable_langfuse(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        cfg = load_trace_config_from_env()
        assert cfg["backend"] == "langfuse"
        assert cfg["public_key"] == "pk-test"
        assert cfg["secret_key"] == "sk-test"

    def test_host_env_var_propagates(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        monkeypatch.setenv("LANGFUSE_HOST", "https://example-host.internal:3000")
        cfg = load_trace_config_from_env()
        assert cfg["host"] == "https://example-host.internal:3000"

    def test_partial_langfuse_keys_falls_back_to_null(self, monkeypatch):
        """Only one of public/secret set — treat as misconfigured, don't
        silently enable with an incomplete pair."""
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        cfg = load_trace_config_from_env()
        assert cfg["backend"] == "null"

    def test_explicit_backend_override(self, monkeypatch):
        """AXIOM_TRACE_BACKEND forces the choice even if Langfuse keys
        are set — useful for testing or turning tracing off at a site."""
        monkeypatch.setenv("AXIOM_TRACE_BACKEND", "null")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
        cfg = load_trace_config_from_env()
        assert cfg["backend"] == "null"

    def test_explicit_in_memory_backend(self, monkeypatch):
        monkeypatch.setenv("AXIOM_TRACE_BACKEND", "in_memory")
        cfg = load_trace_config_from_env()
        assert cfg["backend"] == "in_memory"


# ---------------------------------------------------------------------------
# build_trace_provider_from_env
# ---------------------------------------------------------------------------


class TestBuildFromEnv:
    def test_empty_env_returns_null_provider(self):
        provider = build_trace_provider_from_env()
        from axiom.infra.tracing.null_provider import NullTraceProvider

        assert isinstance(provider, NullTraceProvider)

    def test_in_memory_override(self, monkeypatch):
        monkeypatch.setenv("AXIOM_TRACE_BACKEND", "in_memory")
        provider = build_trace_provider_from_env()
        from axiom.infra.tracing.in_memory_provider import InMemoryTraceProvider

        assert isinstance(provider, InMemoryTraceProvider)

    def test_langfuse_with_mock_transport(self, monkeypatch):
        """Real Langfuse keys PLUS a transport override (for testing).

        In production, the factory instantiates a real `_HttpTransport`
        from the keys + host; in tests we pass a mock so we don't make
        network calls. Every CI run proves the wiring works end-to-end.
        """
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

        class _MockTransport:
            def __init__(self):
                self.batches = []

            def post_batch(self, events):
                self.batches.append(list(events))

        mock_transport = _MockTransport()
        provider = build_trace_provider_from_env(_transport=mock_transport)
        from axiom.infra.tracing.langfuse_provider import LangfuseTraceProvider

        assert isinstance(provider, LangfuseTraceProvider)

        # Exercise the end-to-end path
        tid = provider.start_trace("test.event", foo="bar")
        provider.log_generation(tid, model="gpt-4o", prompt="hi", output="hello")
        provider.flush()

        assert len(mock_transport.batches) == 1
        events = mock_transport.batches[0]
        assert events[0]["type"] == "trace-create"
        assert events[0]["body"]["name"] == "test.event"
        assert events[0]["body"]["metadata"] == {"foo": "bar"}
        assert events[1]["type"] == "generation-create"
        assert events[1]["body"]["model"] == "gpt-4o"
        assert events[1]["body"]["traceId"] == tid

    def test_unknown_backend_falls_back_to_null(self, monkeypatch):
        """Unknown backends never raise — silently fall through to null.
        Prevents a typo in a deployment env var from taking the node down."""
        monkeypatch.setenv("AXIOM_TRACE_BACKEND", "not-a-real-backend")
        provider = build_trace_provider_from_env()
        from axiom.infra.tracing.null_provider import NullTraceProvider

        assert isinstance(provider, NullTraceProvider)


# ---------------------------------------------------------------------------
# Classroom composition_boot integration
# ---------------------------------------------------------------------------


class TestComposeClassroomTracer:
    """The classroom composition helper should surface a trace provider
    configured from env — no classroom caller should have to reach for
    env vars themselves."""

    def test_build_classroom_tracer_uses_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_TRACE_BACKEND", "in_memory")
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.composition_boot import (
            build_classroom_tracer,
        )
        from axiom.infra.tracing.in_memory_provider import InMemoryTraceProvider

        tracer = build_classroom_tracer(
            classroom_id="c1", course_id="course-1",
        )
        # The underlying provider is the env-chosen one.
        assert isinstance(tracer._provider, InMemoryTraceProvider)

    def test_build_classroom_tracer_default_is_null(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
        from axiom.extensions.builtins.classroom.composition_boot import (
            build_classroom_tracer,
        )
        from axiom.infra.tracing.null_provider import NullTraceProvider

        tracer = build_classroom_tracer(
            classroom_id="c1", course_id="course-1",
        )
        assert isinstance(tracer._provider, NullTraceProvider)
