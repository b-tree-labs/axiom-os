# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The composer hands back the stable system part and the turn tail as two parts (RS-P8, ADR-272 D2).

A provider's prompt cache matches a prefix, so anything that changes from one request to the next has
to sit after the history, not before it. The composer therefore knows, for every contribution, whether
it belongs to the session (the stable system part) or to this request alone (the turn tail). A
contribution that does not say is per-request, so a new one cannot break the prefix by accident. The
policy layer (ADR-160) is always in the system part.

``render_text`` is not touched: it is the layout every request has today.
"""

from __future__ import annotations

import pytest

from axiom.infra.prompt_composer import (
    TURN_TAIL_LABEL,
    PromptComposer,
)
from axiom.infra.prompt_overrides import apply_overrides


def _composer(**per_request_content: str) -> PromptComposer:
    """A composer with a stable identity and a per-request screen, retrieved block and clock."""
    c = PromptComposer(count_fn=len)
    c.add("identity", name="who", content="IDENTITY", source="t", scope="session")
    c.add("domain_context", name="house", content="HOUSE-RULES", source="t", scope="session")
    c.add(
        "session_memory",
        name="screen",
        content=per_request_content.get("screen", "SCREEN-1"),
        source="t",
        scope="per_request",
    )
    c.add(
        "retrieved",
        name="rag",
        content=per_request_content.get("rag", "RETRIEVED-1"),
        source="t",
        scope="per_request",
    )
    c.add(
        "live",
        name="clock",
        content=per_request_content.get("clock", "CLOCK-1"),
        source="t",
        scope="per_request",
    )
    return c


def test_an_undeclared_contribution_renders_in_the_turn_tail():
    """RS-P8-A3 at the composer: no declaration means per-request, in any layer."""
    c = PromptComposer(count_fn=len)
    c.add("identity", name="who", content="IDENTITY", source="t", scope="session")
    c.add("domain_context", name="newcomer", content="NEWCOMER", source="t")

    parts = c.render_parts()

    assert "NEWCOMER" in parts.tail
    assert "NEWCOMER" not in parts.system
    assert parts.system == "IDENTITY"


def test_a_session_contribution_renders_in_the_system_part_only():
    parts = _composer().render_parts()

    assert "IDENTITY" in parts.system and "HOUSE-RULES" in parts.system
    assert "IDENTITY" not in parts.tail and "HOUSE-RULES" not in parts.tail


def test_retrieved_live_and_screen_render_in_the_tail_and_never_in_the_system_part():
    parts = _composer().render_parts()

    for marker in ("RETRIEVED-1", "CLOCK-1", "SCREEN-1"):
        assert marker in parts.tail
        assert marker not in parts.system


def test_the_system_part_is_byte_identical_when_only_per_request_content_changes():
    first = _composer().render_parts()
    second = _composer(screen="SCREEN-2", rag="RETRIEVED-2", clock="CLOCK-2").render_parts()

    assert first.system == second.system
    assert first.tail != second.tail


def test_the_tail_is_labelled_as_context_and_keeps_layer_order():
    parts = _composer().render_parts()

    assert parts.tail.startswith(TURN_TAIL_LABEL)
    body = parts.tail[len(TURN_TAIL_LABEL) :]
    assert body.index("SCREEN-1") < body.index("RETRIEVED-1") < body.index("CLOCK-1")


def test_the_policy_layer_stays_in_the_system_part_even_when_declared_per_request():
    """ADR-160: the policy tier is never moved out of the system prompt."""
    c = PromptComposer(count_fn=len)
    c.add("policies", name="rule", content="POLICY", source="t", scope="per_request")
    c.add("policies", name="undeclared", content="POLICY-TWO", source="t")

    parts = c.render_parts()

    assert parts.system == "POLICY\n\nPOLICY-TWO"
    assert parts.tail == ""


def test_a_composer_with_nothing_per_request_has_an_empty_tail_and_no_label():
    c = PromptComposer(count_fn=len)
    c.add("identity", name="who", content="IDENTITY", source="t", scope="session")

    parts = c.render_parts()

    assert parts.tail == ""
    assert TURN_TAIL_LABEL not in parts.system


def test_an_empty_contribution_adds_nothing_to_either_part():
    c = PromptComposer(count_fn=len)
    c.add("identity", name="who", content="IDENTITY", source="t", scope="session")
    c.add("retrieved", name="rag", content="", source="t")

    assert c.render_parts().tail == ""


def test_render_text_is_the_same_whatever_the_scopes_are():
    """The legacy flat string is the one every request has today, and scopes never change it."""
    declared = _composer()
    undeclared = PromptComposer(count_fn=len)
    for contribution in declared.debug():
        undeclared.add(
            contribution.layer, name=contribution.name, content=contribution.content, source="t"
        )

    assert declared.render_text() == undeclared.render_text()
    assert declared.render_text() == "\n\n".join(
        ["IDENTITY", "HOUSE-RULES", "SCREEN-1", "RETRIEVED-1", "CLOCK-1"]
    )


def test_a_scope_that_is_neither_session_nor_per_request_is_refused():
    c = PromptComposer(count_fn=len)
    with pytest.raises(ValueError, match="scope"):
        c.add("identity", name="who", content="X", source="t", scope="sometimes")


def test_an_override_keeps_the_scope_of_the_contribution_it_replaces():
    c = _composer()

    apply_overrides(c, {"who": "NEW-IDENTITY", "rag": "NEW-RETRIEVED"})

    parts = c.render_parts()
    assert "NEW-IDENTITY" in parts.system
    assert "NEW-RETRIEVED" in parts.tail
    assert "NEW-RETRIEVED" not in parts.system
