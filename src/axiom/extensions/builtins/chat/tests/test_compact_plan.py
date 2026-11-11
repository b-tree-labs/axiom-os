# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/compact decides WHAT to summarise before any model is asked (ADR-168, C5).

The unit of compaction is a complete turn (a user message and everything up to the next one), so a tool
call is never separated from its result and the most recent turns stay verbatim.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat.commands_ext import compact as c
from axiom.infra.orchestrator.session import Session

from .compact_helpers import conversation


def test_a_turn_keeps_its_tool_call_and_result_together():
    s = conversation(3, with_tools_in={1})
    turns = c.split_turns(s.messages)
    assert [len(t) for t in turns] == [2, 4, 2]
    assert [m.role for m in turns[1]] == ["user", "assistant", "tool", "assistant"]


def test_keep_n_turns_cuts_on_a_turn_boundary_never_inside_a_tool_pair():
    s = conversation(5, with_tools_in={2, 3})
    plan = c.plan(s, keep_turns=2)
    assert isinstance(plan, c.Plan)
    assert plan.kept[0].role == "user" and plan.kept[0].content.startswith("question 3")
    # the retained window holds the tool call AND its result
    ids = {t["id"] for m in plan.kept for t in m.tool_calls if m.role == "assistant"}
    results = {m.tool_calls[0]["tool_call_id"] for m in plan.kept if m.role == "tool"}
    assert ids == results == {"t3"}
    assert plan.older + plan.kept == s.messages


def test_every_tool_call_in_the_summarised_span_has_its_result_there_too():
    s = conversation(6, with_tools_in={0, 1, 2})
    plan = c.plan(s, keep_turns=2)
    ids = {t["id"] for m in plan.older if m.role == "assistant" for t in m.tool_calls}
    results = {m.tool_calls[0]["tool_call_id"] for m in plan.older if m.role == "tool"}
    assert ids == results and ids


def test_the_default_keeps_what_fits_in_the_token_budget_and_at_least_the_last_turn():
    s = conversation(10, words=100)
    one = c.estimate_tokens(c.split_turns(s.messages)[0])
    plan = c.plan(s, keep_budget=int(one * 2.5))
    assert plan.kept_turns == 2
    tiny = c.plan(s, keep_budget=1)
    assert tiny.kept_turns == 1  # the last turn is never summarised away


def test_one_turn_has_nothing_to_compact_and_the_reason_says_so():
    reason = c.plan(conversation(1))
    assert isinstance(reason, str) and "nothing to compact" in reason.lower()
    assert isinstance(c.plan(Session()), str)


def test_keeping_more_turns_than_exist_is_nothing_to_compact():
    assert isinstance(c.plan(conversation(3), keep_turns=5), str)


def test_a_span_that_is_only_an_earlier_summary_is_not_compacted_again():
    s = conversation(3)
    first = c.plan(s, keep_turns=1)
    s.messages = [c.make_summary_message("abc123", "earlier", replaced_messages=4, replaced_tokens=90,
                                         sources=[])] + first.kept
    reason = c.plan(s, keep_turns=1)
    assert isinstance(reason, str)


def test_token_counts_say_what_they_are():
    assert "estimate" in c.token_method().lower()
    assert c.estimate_tokens(conversation(2).messages) > 0
