# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A capability's answer reaches the person, not only the model.

Tables were the first half. Walking a partner's onboarding through chat,
three of four questions still came back as a checkmark:

    > show me my channels      v daq__table (0.6s)   + the table
    > what can this site do?   v daq__providers (0.6s)
    > is my DAQ set up?        v daq__view (0.6s)
    > what would land?         v daq__preview (0.6s)

The model had every answer. The person had four ticks.

Every skill already renders for a person — that is what `actions_taken`
holds, and it is the same text its CLI prints. Chat shows that rather than
growing a renderer of its own per capability.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.providers.base import (
    VIEW_LINE_BUDGET,
    tool_result_view,
)
from axiom.extensions.builtins.scidisplay.table_spec import tabulate


def _envelope(value, actions=()):
    """The shape `_execute_skill_tool` returns."""
    return {"ok": True, "value": value, "errors": [], "actions_taken": list(actions)}


class TestTheOwnersRenderingIsWhatIsShown:
    def test_a_skills_lines_reach_the_person(self):
        view = tool_result_view(_envelope({"count": 2}, [
            "csv_long: one row per (time, channel, value)",
            "mat_workspace: a MATLAB workspace deposit",
        ]))
        assert "csv_long" in view
        assert "mat_workspace" in view

    def test_a_multi_line_rendering_keeps_its_shape(self):
        """`daq.view` splits a rendered block into lines. Joining them back
        with anything but a newline would reflow a laid-out view."""
        lines = ["SETTINGS", "  no providers configured", "", "FUNCTIONING"]
        assert tool_result_view(_envelope({}, lines)) == "\n".join(lines)

    def test_a_failure_still_says_what_to_do(self):
        """"pass manifest=<path>" is the most useful thing a failed call
        has, and it lived in the field nobody displayed."""
        result = {"ok": False, "value": {"error": "no manifest"},
                  "errors": [], "actions_taken": ["pass manifest=<path to source.toml>"]}
        assert "manifest=" in tool_result_view(result)

    def test_nothing_rendered_stays_compact(self):
        assert tool_result_view(_envelope({"ok": True})) == ""

    def test_trailing_blanks_are_not_content(self):
        assert tool_result_view(_envelope({}, ["one", "", ""])) == "one"


class TestALongAnswerCannotScrollTheConversationAway:
    def test_it_is_cut_at_the_budget(self):
        view = tool_result_view(_envelope({}, [f"line {i}" for i in range(50)]))
        assert len(view.splitlines()) == VIEW_LINE_BUDGET + 1

    def test_it_says_how_much_it_cut(self):
        """Silently truncating is how somebody acts on half an answer
        believing it was all of it."""
        view = tool_result_view(_envelope({}, [f"line {i}" for i in range(50)]))
        assert f"{50 - VIEW_LINE_BUDGET} more line(s)" in view

    def test_exactly_the_budget_is_not_cut(self):
        view = tool_result_view(_envelope({}, [f"line {i}" for i in range(VIEW_LINE_BUDGET)]))
        assert "more line(s)" not in view


class TestATableIsStillShownWhole:
    """A table is the answer. Half of one is a different answer, and a
    sorted page cut at twelve rows reads as a complete top twelve."""

    @pytest.fixture
    def big_table(self):
        rows = [{"channel": f"ch{i:03d}", "value": float(i)} for i in range(40)]
        return tabulate(
            rows,
            columns=(("channel", "channel", True, "left"),
                     ("value", "value", True, "right")),
            title="channels",
            page_size=40,
        )

    def test_it_is_not_truncated(self, big_table):
        view = tool_result_view(_envelope(big_table, ["ignored"]))
        assert "ch039" in view
        assert "more line(s)" not in view

    def test_it_wins_over_the_actions(self, big_table):
        view = tool_result_view(_envelope(big_table, ["something else entirely"]))
        assert "something else entirely" not in view
