# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The pieces of a bound chat turn that need no node: which questions are data questions, which
tools fit a small window, and the verbs a serving node reports it ran. The whole path, through a
real gate to a promoted verb, is ``test_a_bound_chat_answers_with_the_nodes_tools``."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat import bound_tools as bt
from axiom.extensions.builtins.chat.tools import ToolDef
from axiom.infra.orchestrator.actions import ActionCategory
from axiom.llm import gateway as gw


@pytest.mark.parametrize("question", [
    "what power is now, latest one please",
    "temp at outlet last hour?",
    "how high it go yesterday",
    "what does my_view_by_hour say for the last day?",
])
def test_a_question_about_a_value_or_a_recent_state_is_a_data_question(question):
    assert bt.is_data_question(question)


@pytest.mark.parametrize("question", ["hello there, who are you?", "write me a haiku about rain"])
def test_other_questions_are_not(question):
    assert not bt.is_data_question(question)


def test_a_question_names_a_promoted_verb_by_its_name_or_its_words():
    found = bt.BoundTools(site="s", face_url="https://n.example.org",
                          kit_verbs={"my_view_by_hour": "my_view_by_hour", "my_view": "my_view"})
    assert bt.named_kit_verb("what does my_view_by_hour say?", found) == "my_view_by_hour"
    assert bt.named_kit_verb("show me my view by hour", found) == "my_view_by_hour"
    assert bt.named_kit_verb("what is the latest?", found) is None


def _tool(name: str, about: str) -> ToolDef:
    return ToolDef(name=name, description=about, category=ActionCategory.READ,
                   parameters={"type": "object", "properties": {"x": {"type": "string"}}})


def test_tools_that_do_not_fit_are_chosen_by_the_question_and_it_is_said():
    tools = {f"other_{i}": _tool(f"other_{i}", "an unrelated helper " * 20) for i in range(40)}
    tools["data__latest"] = _tool("data__latest", "the newest value of each channel")
    tools["hourly_view"] = _tool("hourly_view", "a promoted verb")
    chosen, note = bt.fit_to_context(tools, "latest hourly view please", window=2600, prompt_chars=0,
                                     model="tiny", prefer=frozenset({"data__latest"}),
                                     keep=frozenset({"hourly_view"}))
    assert list(chosen)[:2] == ["hourly_view", "data__latest"]
    assert len(chosen) < len(tools)
    assert note == (f"offered {len(chosen)} of {len(tools)} tools, chosen by the question: tiny's "
                    "context window (2,600 tokens) cannot hold them all")


def test_every_tool_is_kept_when_they_fit_or_the_window_is_unknown():
    tools = {"a": _tool("a", "x"), "b": _tool("b", "y")}
    assert bt.fit_to_context(tools, "q", window=0, prompt_chars=0) == (tools, "")
    assert bt.fit_to_context(tools, "q", window=100_000, prompt_chars=0) == (tools, "")


def test_the_verbs_a_serving_node_reports_are_read_off_its_response():
    assert gw._served_verbs({"x_verbs_called": [{"verb": "status_at", "args": {}}, "other"]}) == [
        "status_at", "other"]
    assert gw._served_verbs({"choices": []}) == []
