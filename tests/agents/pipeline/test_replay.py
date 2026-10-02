# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ReplayEnvelope per ADR-034 §D9."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.agents.pipeline.replay import (
    CapturedInput,
    EnvelopeBuilder,
    ReplayEnvelope,
    ReplayMode,
    UncapturedInput,
    capture_model_call,
    capture_retrieval,
    capture_tool_invocation,
    envelopes_equivalent,
)

# ---------------------------------------------------------------------------
# Builder fluent API
# ---------------------------------------------------------------------------

class TestEnvelopeBuilder:
    def test_capture_declare_gap_build_yields_frozen_envelope(self):
        envelope = (
            EnvelopeBuilder()
            .capture("model", "qwen-3.5", source="config")
            .capture("temperature", 0.0, source="config")
            .declare_gap("wall_clock", "real-time clock", severity="informational")
            .build()
        )
        assert isinstance(envelope, ReplayEnvelope)
        assert envelope.mode is ReplayMode.BEST_EFFORT
        assert len(envelope.captured) == 2
        assert len(envelope.not_captured) == 1
        # Frozen — assignment must fail.
        with pytest.raises(Exception):  # noqa: B017
            envelope.fingerprint = "tampered"  # type: ignore[misc]
        # Items are tuples not lists.
        assert isinstance(envelope.captured, tuple)
        assert isinstance(envelope.not_captured, tuple)
        assert isinstance(envelope.captured[0], CapturedInput)
        assert isinstance(envelope.not_captured[0], UncapturedInput)

    def test_envelope_id_auto_generated_unique(self):
        e1 = EnvelopeBuilder().capture("x", 1).build()
        e2 = EnvelopeBuilder().capture("x", 1).build()
        assert e1.envelope_id != e2.envelope_id
        assert len(e1.envelope_id) == 32  # uuid4().hex

    def test_capture_records_source_and_timestamp(self):
        envelope = (
            EnvelopeBuilder()
            .capture("model", "qwen", source="config")
            .build()
        )
        ci = envelope.captured[0]
        assert ci.name == "model"
        assert ci.value == "qwen"
        assert ci.source == "config"
        assert isinstance(ci.captured_at, datetime)


# ---------------------------------------------------------------------------
# Mode behaviour
# ---------------------------------------------------------------------------

class TestDeterministicStrictMode:
    def test_strict_raises_on_blocker_gap(self):
        builder = (
            EnvelopeBuilder(mode=ReplayMode.DETERMINISTIC_STRICT)
            .capture("model", "qwen")
            .declare_gap(
                "external_api_state",
                "remote service had no replay handle",
                severity="blocker",
            )
        )
        with pytest.raises(ValueError, match="blocker"):
            builder.build()

    def test_strict_does_not_raise_on_informational_gap(self):
        envelope = (
            EnvelopeBuilder(mode=ReplayMode.DETERMINISTIC_STRICT)
            .capture("model", "qwen")
            .declare_gap("note", "FYI only", severity="informational")
            .build()
        )
        assert envelope.mode is ReplayMode.DETERMINISTIC_STRICT
        assert envelope.not_captured[0].severity == "informational"

    def test_strict_does_not_raise_on_warning_gap(self):
        envelope = (
            EnvelopeBuilder(mode=ReplayMode.DETERMINISTIC_STRICT)
            .capture("model", "qwen")
            .declare_gap("hint", "best-effort still ok", severity="warning")
            .build()
        )
        assert envelope.not_captured[0].severity == "warning"

    def test_best_effort_does_not_raise_on_blocker(self):
        envelope = (
            EnvelopeBuilder(mode=ReplayMode.BEST_EFFORT)
            .capture("model", "qwen")
            .declare_gap("external", "api state", severity="blocker")
            .build()
        )
        assert envelope.not_captured[0].severity == "blocker"

    def test_invalid_severity_rejected_at_declare(self):
        builder = EnvelopeBuilder()
        with pytest.raises(ValueError, match="severity"):
            builder.declare_gap("x", "bad", severity="critical")


# ---------------------------------------------------------------------------
# Fingerprint properties
# ---------------------------------------------------------------------------

class TestFingerprint:
    def test_same_inputs_same_fingerprint(self):
        e1 = (
            EnvelopeBuilder()
            .capture("model", "qwen")
            .capture("temperature", 0.0)
            .build()
        )
        e2 = (
            EnvelopeBuilder()
            .capture("model", "qwen")
            .capture("temperature", 0.0)
            .build()
        )
        assert e1.fingerprint == e2.fingerprint

    def test_changing_value_changes_fingerprint(self):
        e1 = EnvelopeBuilder().capture("model", "qwen").build()
        e2 = EnvelopeBuilder().capture("model", "llama").build()
        assert e1.fingerprint != e2.fingerprint

    def test_changing_temperature_changes_fingerprint(self):
        e1 = EnvelopeBuilder().capture("temperature", 0.0).build()
        e2 = EnvelopeBuilder().capture("temperature", 0.7).build()
        assert e1.fingerprint != e2.fingerprint

    def test_dict_insertion_order_does_not_matter(self):
        e1 = EnvelopeBuilder().capture("cfg", {"a": 1, "b": 2}).build()
        e2 = EnvelopeBuilder().capture("cfg", {"b": 2, "a": 1}).build()
        assert e1.fingerprint == e2.fingerprint

    def test_capture_order_does_not_matter(self):
        # Captures sorted lexicographically by name in canonical form.
        e1 = (
            EnvelopeBuilder()
            .capture("alpha", 1)
            .capture("beta", 2)
            .build()
        )
        e2 = (
            EnvelopeBuilder()
            .capture("beta", 2)
            .capture("alpha", 1)
            .build()
        )
        assert e1.fingerprint == e2.fingerprint

    def test_tuple_serialized_as_list(self):
        # Tuples and lists with the same elements should fingerprint identically.
        e1 = EnvelopeBuilder().capture("ids", ["a", "b", "c"]).build()
        e2 = EnvelopeBuilder().capture("ids", ("a", "b", "c")).build()
        assert e1.fingerprint == e2.fingerprint

    def test_fingerprint_is_sha256_hex(self):
        envelope = EnvelopeBuilder().capture("x", 1).build()
        assert len(envelope.fingerprint) == 64
        int(envelope.fingerprint, 16)  # valid hex

    def test_fingerprint_excludes_timestamp(self):
        # Two envelopes built at different times with same data should match.
        import time
        e1 = EnvelopeBuilder().capture("x", 1).build()
        time.sleep(0.001)
        e2 = EnvelopeBuilder().capture("x", 1).build()
        assert e1.fingerprint == e2.fingerprint
        # ... but creation timestamps differ.
        assert e1.created_at != e2.created_at or e1.envelope_id != e2.envelope_id


# ---------------------------------------------------------------------------
# JSON-serializability guard
# ---------------------------------------------------------------------------

class TestNonSerializable:
    def test_class_instance_value_raises_at_capture_time(self):
        class Opaque:
            pass

        builder = EnvelopeBuilder()
        with pytest.raises((TypeError, ValueError)):
            builder.capture("bad", Opaque())

    def test_function_value_raises_at_capture_time(self):
        builder = EnvelopeBuilder()
        with pytest.raises((TypeError, ValueError)):
            builder.capture("bad", lambda: None)

    def test_set_value_raises(self):
        builder = EnvelopeBuilder()
        with pytest.raises((TypeError, ValueError)):
            builder.capture("bad", {1, 2, 3})

    def test_datetime_value_accepted(self):
        # Datetimes are explicitly canonicalized.
        envelope = (
            EnvelopeBuilder()
            .capture("when", datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC))
            .build()
        )
        assert envelope.captured[0].name == "when"

    def test_nested_serializable_accepted(self):
        envelope = (
            EnvelopeBuilder()
            .capture(
                "complex",
                {"a": [1, 2, {"b": True}], "c": None, "d": 1.5},
            )
            .build()
        )
        assert envelope.captured[0].name == "complex"


# ---------------------------------------------------------------------------
# envelopes_equivalent
# ---------------------------------------------------------------------------

class TestEnvelopesEquivalent:
    def test_identical_inputs_compare_equivalent(self):
        e1 = (
            EnvelopeBuilder()
            .capture("model", "qwen")
            .capture("temperature", 0.0)
            .declare_gap("clock", "rt", severity="informational")
            .build()
        )
        e2 = (
            EnvelopeBuilder()
            .capture("model", "qwen")
            .capture("temperature", 0.0)
            .declare_gap("clock", "rt", severity="informational")
            .build()
        )
        ok, diff = envelopes_equivalent(e1, e2)
        assert ok, diff
        assert diff == "" or "match" in diff.lower() or diff is None or diff == ""

    def test_different_capture_value_not_equivalent_with_diff(self):
        e1 = EnvelopeBuilder().capture("model", "qwen").build()
        e2 = EnvelopeBuilder().capture("model", "llama").build()
        ok, diff = envelopes_equivalent(e1, e2)
        assert not ok
        assert "model" in diff or "fingerprint" in diff.lower() or "captur" in diff.lower()
        assert diff  # non-empty

    def test_different_modes_not_equivalent(self):
        e1 = EnvelopeBuilder(mode=ReplayMode.BEST_EFFORT).capture("x", 1).build()
        e2 = EnvelopeBuilder(mode=ReplayMode.DETERMINISTIC_STRICT).capture("x", 1).build()
        ok, diff = envelopes_equivalent(e1, e2)
        assert not ok
        assert "mode" in diff.lower()

    def test_ignore_timestamps_default_true(self):
        # Even when timestamps differ, equivalent if the captured values match.
        import time
        e1 = EnvelopeBuilder().capture("x", 1).build()
        time.sleep(0.001)
        e2 = EnvelopeBuilder().capture("x", 1).build()
        ok, diff = envelopes_equivalent(e1, e2, ignore_timestamps=True)
        assert ok, diff

    def test_different_gap_makes_not_equivalent(self):
        e1 = (
            EnvelopeBuilder()
            .capture("x", 1)
            .declare_gap("clock", "rt", severity="informational")
            .build()
        )
        e2 = EnvelopeBuilder().capture("x", 1).build()
        ok, diff = envelopes_equivalent(e1, e2)
        assert not ok
        assert "gap" in diff.lower() or "captur" in diff.lower() or "not_captured" in diff.lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestCaptureModelCall:
    def test_captures_all_deterministic_fields(self):
        builder = EnvelopeBuilder()
        capture_model_call(
            builder,
            provider="openai",
            model="gpt-4",
            temperature=0.0,
            system_prompt="you are helpful",
            user_prompt="hi",
        )
        envelope = builder.build()
        names = {c.name for c in envelope.captured}
        assert "model.provider" in names
        assert "model.model" in names
        assert "model.temperature" in names
        assert "model.system_prompt" in names
        assert "model.user_prompt" in names

    def test_none_system_prompt_captured_as_null(self):
        builder = EnvelopeBuilder()
        capture_model_call(
            builder,
            provider="openai",
            model="gpt-4",
            temperature=0.0,
            system_prompt=None,
            user_prompt="hi",
        )
        envelope = builder.build()
        captured_map = {c.name: c.value for c in envelope.captured}
        assert captured_map["model.system_prompt"] is None

    def test_returns_builder_for_chaining(self):
        builder = EnvelopeBuilder()
        result = capture_model_call(
            builder,
            provider="p",
            model="m",
            temperature=0.0,
            system_prompt=None,
            user_prompt="x",
        )
        assert result is builder


class TestCaptureRetrieval:
    def test_captures_query_and_fragment_set(self):
        builder = EnvelopeBuilder()
        capture_retrieval(
            builder,
            query="what is plutonium",
            fragment_ids=["frag-1", "frag-2", "frag-3"],
            scores=[0.9, 0.7, 0.5],
        )
        envelope = builder.build()
        names = {c.name for c in envelope.captured}
        assert "retrieval.query" in names
        assert "retrieval.fragment_ids" in names
        assert "retrieval.scores" in names

    def test_returns_builder_for_chaining(self):
        builder = EnvelopeBuilder()
        result = capture_retrieval(
            builder, query="q", fragment_ids=[], scores=[]
        )
        assert result is builder


class TestCaptureToolInvocation:
    def test_captures_tool_id_version_args(self):
        builder = EnvelopeBuilder()
        capture_tool_invocation(
            builder,
            tool_id="search",
            tool_version="1.2.3",
            input_args={"query": "hello", "limit": 10},
        )
        envelope = builder.build()
        names = {c.name for c in envelope.captured}
        assert "tool.id" in names
        assert "tool.version" in names
        assert "tool.input_args" in names

    def test_returns_builder_for_chaining(self):
        builder = EnvelopeBuilder()
        result = capture_tool_invocation(
            builder, tool_id="t", tool_version="1", input_args={}
        )
        assert result is builder

    def test_non_serializable_input_arg_raises(self):
        class Opaque:
            pass

        builder = EnvelopeBuilder()
        with pytest.raises((TypeError, ValueError)):
            capture_tool_invocation(
                builder,
                tool_id="t",
                tool_version="1",
                input_args={"bad": Opaque()},
            )


# ---------------------------------------------------------------------------
# End-to-end replay scenario
# ---------------------------------------------------------------------------

class TestEndToEndReplay:
    def test_full_step_envelope_roundtrip(self):
        """Simulate capturing a complete model+retrieval+tool step."""
        builder = EnvelopeBuilder(mode=ReplayMode.BEST_EFFORT)
        capture_model_call(
            builder,
            provider="local",
            model="qwen-3.5",
            temperature=0.0,
            system_prompt="You are a tutor.",
            user_prompt="Explain neutron flux.",
        )
        capture_retrieval(
            builder,
            query="neutron flux",
            fragment_ids=["f1", "f2"],
            scores=[0.92, 0.81],
        )
        capture_tool_invocation(
            builder,
            tool_id="search",
            tool_version="1.0.0",
            input_args={"top_k": 5},
        )
        builder.declare_gap(
            "wall_clock",
            "step ran at runtime; not captured",
            severity="informational",
        )
        e1 = builder.build()

        # Replay produces an equivalent envelope.
        builder2 = EnvelopeBuilder(mode=ReplayMode.BEST_EFFORT)
        capture_model_call(
            builder2,
            provider="local",
            model="qwen-3.5",
            temperature=0.0,
            system_prompt="You are a tutor.",
            user_prompt="Explain neutron flux.",
        )
        capture_retrieval(
            builder2,
            query="neutron flux",
            fragment_ids=["f1", "f2"],
            scores=[0.92, 0.81],
        )
        capture_tool_invocation(
            builder2,
            tool_id="search",
            tool_version="1.0.0",
            input_args={"top_k": 5},
        )
        builder2.declare_gap(
            "wall_clock",
            "step ran at runtime; not captured",
            severity="informational",
        )
        e2 = builder2.build()

        ok, diff = envelopes_equivalent(e1, e2)
        assert ok, diff
        assert e1.fingerprint == e2.fingerprint
