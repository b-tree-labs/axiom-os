# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/compact: a real, tier-enforced, reversible, auditable summary (ADR-168, C5).

A real ``Gateway`` and real providers talk HTTP to a local OpenAI-compatible server; only the network
boundary is faked. Each test names the property it would fail on.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands_ext import compact as c
from axiom.extensions.builtins.chat.where import note_content_tier

from .compact_helpers import FakeLLM, agent_with, conversation, provider


@pytest.fixture
def llm():
    s = FakeLLM()
    yield s
    s.close()


@pytest.fixture
def other():
    s = FakeLLM(reply="SUMMARY from the other endpoint")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _audit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path / "runtime"))


def _run(agent, line="/compact", store=None):
    from axiom.extensions.builtins.chat.commands_ext import load_all

    load_all()
    from axiom.extensions.builtins.chat import builtin_commands  # noqa: F401

    return REGISTRY.dispatch(line, CommandContext(agent=agent, store=store, line=line))


def _agent(monkeypatch, llm, *, session=None, tier="any"):
    return agent_with([provider("local", llm.endpoint, tier=tier)], monkeypatch, session)


def test_the_stub_is_gone_and_the_command_is_registered_once():
    from axiom.extensions.builtins.chat import commands

    assert not hasattr(commands, "cmd_compact")
    from axiom.extensions.builtins.chat.commands_ext import load_all

    load_all()
    cmd = REGISTRY.get("/compact")
    assert cmd is not None and cmd.mutates
    assert {"keep", "undo", "preview"} <= set(cmd.subcommands)


def test_compacts_older_turns_into_one_marked_summary_and_keeps_recent_turns_verbatim(
    monkeypatch, llm
):
    s = conversation(6, with_tools_in={1})
    before = list(s.messages)
    agent = _agent(monkeypatch, llm, session=s)
    out = _run(agent, "/compact keep 2")
    kept = before[-4:]
    assert s.messages[1:] == kept and all(a is b for a, b in zip(s.messages[1:], kept))  # verbatim
    head = s.messages[0]
    assert head.content.startswith(c.SUMMARY_MARK)
    assert "SUMMARY: the user asked things" in head.content
    assert f"replaces {len(before) - 4} messages" in head.content
    assert head.role == "user"  # a system-role message is stripped by the Anthropic path
    assert "Compacted" in out


def test_the_model_sees_the_older_span_and_not_the_retained_window(monkeypatch, llm):
    s = conversation(5)
    _run(_agent(monkeypatch, llm, session=s), "/compact keep 1")
    assert "question 0" in llm.prompts and "answer 3" in llm.prompts
    assert "question 4" not in llm.prompts and "answer 4" not in llm.prompts


def test_tool_results_in_the_summarised_span_are_shown_to_the_summariser(monkeypatch, llm):
    s = conversation(4, with_tools_in={0})
    _run(_agent(monkeypatch, llm, session=s), "/compact keep 1")
    assert '"unit": "kg"' in llm.prompts and "lookup" in llm.prompts


def test_before_after_in_messages_and_estimated_tokens_and_where_it_ran(monkeypatch, llm):
    s = conversation(6)
    n = len(s.messages)
    out = _run(_agent(monkeypatch, llm, session=s), "/compact keep 2")
    assert f"{n} messages" in out and f"{len(s.messages)} messages" in out
    assert "estimate" in out.lower() and "tokens" in out
    assert "LOCAL" in out and "local" in out and "m" in out  # placement label + provider name
    assert "/compact undo" in out


def test_usage_of_the_summary_call_is_recorded(monkeypatch, llm):
    agent = _agent(monkeypatch, llm, session=conversation(5))
    _run(agent, "/compact keep 1")
    assert len(agent.usage_recorded) == 1


# ---- tier policy --------------------------------------------------------------------------------


def test_controlled_content_is_summarised_only_by_a_provider_that_admits_it(
    monkeypatch, llm, other
):
    s = conversation(5)
    note_content_tier(s, "export_controlled")
    agent = agent_with(
        [
            provider("cloud", llm.endpoint, tier="any", priority=1),
            provider("cleared", other.endpoint, tier="export_controlled", priority=9),
        ],
        monkeypatch,
        s,
    )
    agent.session.principal_id = "@tester"
    out = _run(agent, "/compact keep 1")
    assert llm.requests == [], "controlled content reached a provider that does not admit it"
    assert other.requests and "other endpoint" in s.messages[0].content
    assert "cleared" in out


def test_with_no_admitting_provider_it_refuses_with_the_reason_and_changes_nothing(
    monkeypatch, llm
):
    s = conversation(5)
    note_content_tier(s, "export_controlled")
    before = list(s.messages)
    out = _run(_agent(monkeypatch, llm, session=s, tier="public"), "/compact keep 1")
    assert llm.requests == []
    assert s.messages == before and c.COMPACTIONS_KEY not in s.context
    assert "export_controlled" in out and "admit" in out.lower()


def test_the_quick_role_model_is_preferred_when_a_provider_serves_it(monkeypatch, llm, other):
    from axiom.llm.local_models import QUICK

    s = conversation(5)
    agent = agent_with(
        [
            provider("big", llm.endpoint, model="big-model", priority=1),
            provider("small", other.endpoint, model=QUICK, priority=9),
        ],
        monkeypatch,
        s,
    )
    _run(agent, "/compact keep 1")
    assert other.requests and not llm.requests
    assert other.requests[0]["model"] == QUICK


# ---- failure honesty ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply,status,expect",
    [("", 200, "empty"), ("   \n", 200, "empty"), ("x", 500, "failed")],
)
def test_a_failed_or_empty_summary_changes_nothing_and_says_why(
    monkeypatch, reply, status, expect
):
    srv = FakeLLM(reply=reply, status=status)
    try:
        s = conversation(5)
        snapshot = [m.to_dict() for m in s.messages]
        out = _run(_agent(monkeypatch, srv, session=s), "/compact keep 1")
        assert [m.to_dict() for m in s.messages] == snapshot
        assert c.COMPACTIONS_KEY not in s.context
        assert expect in out.lower() and "unchanged" in out.lower()
    finally:
        srv.close()


def test_a_summary_not_smaller_than_what_it_replaces_is_refused(monkeypatch):
    srv = FakeLLM(reply="word " * 5000)
    try:
        s = conversation(4, words=10)
        n = len(s.messages)
        out = _run(_agent(monkeypatch, srv, session=s), "/compact keep 1")
        assert len(s.messages) == n and "not smaller" in out.lower()
    finally:
        srv.close()


def test_nothing_to_compact_makes_no_model_call(monkeypatch, llm):
    out = _run(_agent(monkeypatch, llm, session=conversation(1)))
    assert llm.requests == [] and "nothing to compact" in out.lower()


def test_preview_says_what_would_happen_without_calling_a_model(monkeypatch, llm):
    s = conversation(6)
    snapshot = [m.to_dict() for m in s.messages]
    out = _run(_agent(monkeypatch, llm, session=s), "/compact preview keep 2")
    assert llm.requests == [] and [m.to_dict() for m in s.messages] == snapshot
    assert "would" in out.lower() and "local" in out


def test_a_bad_keep_argument_is_refused_not_guessed(monkeypatch, llm):
    out = _run(_agent(monkeypatch, llm, session=conversation(5)), "/compact keep banana")
    assert "usage" in out.lower() and llm.requests == []


# ---- reversibility ------------------------------------------------------------------------------


def test_undo_restores_the_original_messages_exactly(monkeypatch, llm):
    s = conversation(6, with_tools_in={0, 2})
    original = [m.to_dict() for m in s.messages]
    agent = _agent(monkeypatch, llm, session=s)
    _run(agent, "/compact keep 2")
    assert len(s.messages) < len(original)
    out = _run(agent, "/compact undo")
    assert [m.to_dict() for m in s.messages] == original
    assert "Restored" in out and not s.context.get(c.COMPACTIONS_KEY)


def test_turns_added_after_compaction_survive_an_undo(monkeypatch, llm):
    s = conversation(5)
    agent = _agent(monkeypatch, llm, session=s)
    _run(agent, "/compact keep 1")
    s.add_message("user", "later question")
    s.add_message("assistant", "later answer")
    _run(agent, "/compact undo")
    assert [m.content for m in s.messages[-2:]] == ["later question", "later answer"]
    assert s.messages[0].content.startswith("question 0")


def test_two_compactions_undo_one_at_a_time_in_reverse(monkeypatch, llm):
    s = conversation(8)
    agent = _agent(monkeypatch, llm, session=s)
    _run(agent, "/compact keep 4")
    mid = [m.to_dict() for m in s.messages]
    for i in range(3):
        s.add_message("user", f"more {i} " + "gamma " * 80)
        s.add_message("assistant", f"reply {i} " + "delta " * 80)
    mid = [m.to_dict() for m in s.messages]
    _run(agent, "/compact keep 2")
    assert len(s.messages) < len(mid)
    _run(agent, "/compact undo")
    assert [m.to_dict() for m in s.messages] == mid


def test_undo_with_nothing_to_undo_says_so(monkeypatch, llm):
    assert "nothing to undo" in _run(
        _agent(monkeypatch, llm, session=conversation(2)), "/compact undo"
    ).lower()


def test_undo_refuses_if_the_summary_is_no_longer_in_the_session(monkeypatch, llm):
    s = conversation(5)
    agent = _agent(monkeypatch, llm, session=s)
    _run(agent, "/compact keep 1")
    s.messages.clear()  # e.g. /clear
    out = _run(agent, "/compact undo")
    assert "no longer" in out.lower() and s.messages == []


# ---- provenance ---------------------------------------------------------------------------------


def test_sources_cited_in_the_compacted_span_survive_in_the_summary(monkeypatch, llm):
    s = conversation(5)
    ans = s.messages[1]  # an assistant answer inside the span
    s.context["answer_sources"] = {
        ans.timestamp: [
            {"key": "C1", "path": "/docs/handbook.md", "title": "Handbook", "corpus": "org"}
        ]
    }
    ans.content += " see [C1]"
    s.messages[3].content += " https://example.org/ref/42"
    _run(_agent(monkeypatch, llm, session=s), "/compact keep 1")
    head = s.messages[0].content
    assert "Sources" in head and "/docs/handbook.md" in head and "Handbook" in head
    assert "https://example.org/ref/42" in head


def test_citation_markers_that_cannot_be_resolved_are_counted_not_dropped_silently(
    monkeypatch, llm
):
    s = conversation(5)
    s.messages[1].content += " per [C2] and [C3]"
    _run(_agent(monkeypatch, llm, session=s), "/compact keep 1")
    assert "2 citation markers" in s.messages[0].content


def test_a_second_compaction_carries_forward_the_first_ones_sources(monkeypatch, llm):
    s = conversation(6)
    s.messages[1].content += " https://example.org/first"
    agent = _agent(monkeypatch, llm, session=s)
    _run(agent, "/compact keep 3")
    for i in range(3):
        s.add_message("user", f"more {i} " + "gamma " * 80)
        s.add_message("assistant", f"reply {i} " + "delta " * 80)
    _run(agent, "/compact keep 1")
    assert s.messages[0].content.count(c.SUMMARY_MARK) == 1
    assert "https://example.org/first" in s.messages[0].content


# ---- audit --------------------------------------------------------------------------------------


def test_the_compaction_is_written_to_the_audit_record_without_the_conversation(
    monkeypatch, llm, tmp_path
):
    s = conversation(5)
    agent = _agent(monkeypatch, llm, session=s)
    _run(agent, "/compact keep 1")
    _run(agent, "/compact undo")
    f = tmp_path / "runtime" / "sessions" / s.session_id / "prompt_compositions.jsonl"
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    assert [r["action"] for r in rows] == ["compact", "undo"]
    r = rows[0]
    assert r["fact_kind"] == "chat_compaction" and r["replaced_messages"] > 0
    assert r["provider"] == "local" and r["placement"] == "local" and r["token_method"]
    assert "summary" not in r  # would be injected into future prompts by session_memory
    assert "alpha" not in json.dumps(r) and "SUMMARY" not in json.dumps(r)


def test_the_audit_goes_to_the_memory_composition_when_chat_has_one(monkeypatch, llm):
    written = []

    class Composition:
        def write(self, **kw):
            written.append(kw)

    agent = _agent(monkeypatch, llm, session=conversation(5))
    agent._composition = Composition()
    _run(agent, "/compact keep 1")
    assert written and written[0]["content"]["fact_kind"] == "chat_compaction"
    assert written[0]["cognitive_type"] == "episodic"
