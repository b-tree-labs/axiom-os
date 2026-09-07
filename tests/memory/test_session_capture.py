# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.memory.session_capture — the common cross-tool write path.

This module is the load-bearing convergence point. The MCP `memory.append`
tool, `axi memory record` CLI, `axi memory ingest` backstop, and `axi chat`
all call `record_session_turn()`. These tests verify that whoever calls it,
the resulting fragment carries identical provenance / typing / scoping.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Isolated CompositionService fixture — tmp_path-scoped so tests never
# touch ~/.axi/memory/.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_composition(tmp_path: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory"
    base.mkdir()
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


# ---------------------------------------------------------------------------
# record_session_turn — basic behavior
# ---------------------------------------------------------------------------


def test_record_session_turn_writes_episodic_fragment(isolated_composition):
    from axiom.memory.session_capture import record_session_turn

    frag = record_session_turn(
        composition=isolated_composition,
        principal_id="user@example.org",
        tool="claude-code",
        model="opus-4-7",
        user_input="What is the threshold?",
        assistant_output="The system reaches steady state when the ratio = 1.",
    )

    assert frag.cognitive_type.value == "episodic"
    assert frag.provenance.principal_id == "user@example.org"
    assert "claude-code:opus-4-7" in frag.provenance.agents
    assert frag.content["fact_kind"] == "chat_turn"
    assert frag.content["tool"] == "claude-code"
    assert frag.content["model"] == "opus-4-7"
    assert frag.content["user_input"] == "What is the threshold?"
    assert frag.content["assistant_output"].startswith("The system reaches steady state")


def test_record_session_turn_principal_appears_in_show(isolated_composition):
    """End-to-end: write via session_capture, read via session_summary path."""
    from axiom.memory.session_capture import record_session_turn
    from axiom.memory.session_summary import list_fragments_by_principal

    record_session_turn(
        composition=isolated_composition,
        principal_id="user@example.org",
        tool="claude-code",
        model="opus-4-7",
        user_input="hi",
        assistant_output="hello",
    )

    frags = list_fragments_by_principal(
        isolated_composition, "user@example.org", limit=10,
    )
    assert len(frags) == 1
    assert frags[0].content["fact_kind"] == "chat_turn"


def test_record_session_turn_tool_distinguishable_via_agents(isolated_composition):
    """Provenance must distinguish originating tool — the cross-vendor invariant."""
    from axiom.memory.session_capture import record_session_turn
    from axiom.memory.session_summary import list_fragments_by_principal

    record_session_turn(
        composition=isolated_composition, principal_id="user@example.org",
        tool="claude-code", model="opus-4-7",
        user_input="A", assistant_output="B",
    )
    record_session_turn(
        composition=isolated_composition, principal_id="user@example.org",
        tool="chatgpt", model="gpt-4",
        user_input="C", assistant_output="D",
    )
    record_session_turn(
        composition=isolated_composition, principal_id="user@example.org",
        tool="axi-chat", model=None,
        user_input="E", assistant_output="F",
    )

    frags = list_fragments_by_principal(
        isolated_composition, "user@example.org", limit=10,
    )
    tools = {f.content["tool"] for f in frags}
    assert tools == {"claude-code", "chatgpt", "axi-chat"}

    # Agents must encode the tool — needed for filtering by origin.
    agent_strs = {next(iter(f.provenance.agents)) for f in frags}
    assert "claude-code:opus-4-7" in agent_strs
    assert "chatgpt:gpt-4" in agent_strs
    assert "axi-chat" in agent_strs   # no model → bare tool name


def test_record_session_turn_summary_used_when_provided(isolated_composition):
    from axiom.memory.session_capture import record_session_turn

    frag = record_session_turn(
        composition=isolated_composition,
        principal_id="user@example.org",
        tool="claude-code",
        user_input="long question " * 20,
        assistant_output="long answer " * 20,
        summary="User asked about the domain model; covered the ratio.",
    )
    assert frag.content["summary"] == (
        "User asked about the domain model; covered the ratio."
    )


def test_record_session_turn_default_summary_truncates(isolated_composition):
    from axiom.memory.session_capture import record_session_turn

    frag = record_session_turn(
        composition=isolated_composition,
        principal_id="user@example.org",
        tool="claude-code",
        user_input="x" * 500,
        assistant_output="y" * 500,
    )
    # Default summary should be present and bounded.
    assert frag.content["summary"]
    assert len(frag.content["summary"]) < 500


def test_record_session_turn_event_time_override(isolated_composition):
    from axiom.memory.session_capture import record_session_turn

    frag = record_session_turn(
        composition=isolated_composition,
        principal_id="user@example.org",
        tool="claude-code",
        user_input="hi",
        assistant_output="hello",
        event_time="2026-01-01T00:00:00+00:00",
    )
    assert frag.content["event_time"] == "2026-01-01T00:00:00+00:00"


def test_record_session_turn_extra_metadata_passes_through(isolated_composition):
    from axiom.memory.session_capture import record_session_turn

    frag = record_session_turn(
        composition=isolated_composition,
        principal_id="user@example.org",
        tool="claude-code",
        user_input="hi",
        assistant_output="hello",
        extra={"session_id": "abc-123", "host": "ghostty"},
    )
    assert frag.content["extra"]["session_id"] == "abc-123"
    assert frag.content["extra"]["host"] == "ghostty"
