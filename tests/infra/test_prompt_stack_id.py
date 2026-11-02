# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A prompt design needs a name before it can be compared to another one.

Observability already says which contributions composed any single prompt, so an
answer is attributable. Grouping is the missing direction: two thousand turns
that shared a design cannot be measured against two thousand that shared another
unless both designs have a stable id.

These tests pin the four properties that make such an id usable — and the one
that makes it useless if it is got wrong, which is including per-turn content.
"""

from __future__ import annotations

from axiom.infra.prompt_composer import LayerContribution
from axiom.infra.prompt_stack_id import (
    describe_stack,
    fragment_id,
    stack_fragments,
    stack_id,
)


def _c(layer: str, name: str, content: str = "text", source: str = "test"):
    return LayerContribution(layer=layer, name=name, content=content, source=source)


def _design():
    return [
        _c("identity", "persona", "You are careful."),
        _c("capabilities", "tools", "You can search."),
        _c("policies", "safety", "Refuse unsafe requests."),
    ]


# --- the id identifies a design ---------------------------------------------

def test_the_same_design_hashes_the_same_every_time():
    assert stack_id(_design()) == stack_id(_design())


def test_an_edited_fragment_is_a_different_design():
    """Otherwise an experiment compares a prompt against its own later revision
    and reports the difference as noise."""
    changed = _design()
    changed[0] = _c("identity", "persona", "You are reckless.")
    assert stack_id(changed) != stack_id(_design())


def test_a_different_order_is_a_different_design():
    """The same fragments in another order compose a different prompt."""
    a = [_c("capabilities", "one", "A"), _c("capabilities", "two", "B")]
    b = [_c("capabilities", "two", "B"), _c("capabilities", "one", "A")]
    assert stack_id(a) != stack_id(b)


def test_adding_a_fragment_is_a_different_design():
    more = [*_design(), _c("policies", "extra", "Also cite sources.")]
    assert stack_id(more) != stack_id(_design())


# --- the property that makes or breaks it -----------------------------------

def test_per_turn_content_does_not_change_the_id():
    """The whole point. Retrieved context and live state differ every turn;
    hashing them would give every turn a unique id and make grouping — the
    reason this exists — impossible."""
    base = _design()
    turn_one = [*base, _c("retrieved", "chunks", "soil report A"),
                _c("live", "clock", "09:00")]
    turn_two = [*base, _c("retrieved", "chunks", "irradiation log B"),
                _c("live", "clock", "17:42")]

    assert stack_id(turn_one) == stack_id(turn_two) == stack_id(base)


def test_layer_order_not_list_order_decides_composition_order():
    """A caller assembling layers out of order must still get the composed id."""
    scrambled = [
        _c("policies", "safety", "Refuse unsafe requests."),
        _c("identity", "persona", "You are careful."),
        _c("capabilities", "tools", "You can search."),
    ]
    assert stack_id(scrambled) == stack_id(_design())


# --- readability ------------------------------------------------------------

def test_a_fragment_id_names_what_it_is_and_which_version():
    fid = fragment_id(_c("identity", "persona", "You are careful."))
    assert fid.startswith("identity/persona@")
    other = fragment_id(_c("identity", "persona", "You are reckless."))
    assert fid != other, "an edited fragment kept the same id"


def test_describe_carries_the_fragments_not_just_the_hash():
    """An experiment recording only a hash can group results but not explain one."""
    described = describe_stack(_design())
    assert described["stack_id"] == stack_id(_design())
    assert described["fragment_count"] == 3
    assert any(f.startswith("identity/persona@") for f in described["fragments"])


def test_an_empty_stack_has_a_stable_obviously_unreal_id():
    assert stack_id([]) == "empty"
    assert stack_fragments([]) == []


# --- wired into what actually records turns ---------------------------------

def test_the_composer_reports_a_stack_id_on_every_turn():
    """A library nothing calls is a library that drifts.

    The id has to arrive in the record that is already written per turn, or an
    experiment would need a separate collection path and the two would disagree.
    """
    from axiom.infra.prompt_composer import PromptComposer

    composer = PromptComposer()
    composer.add("identity", name="persona", content="You are careful.", source="test")
    composer.add("capabilities", name="tools", content="You can search.", source="test")

    payload = composer.observability_payload()
    assert payload["stack_id"] != "empty"
    assert any(f.startswith("identity/persona@") for f in payload["stack_fragments"])


def test_two_turns_with_different_retrieved_context_share_a_stack_id():
    """The grouping property, through the real composer rather than a list."""
    from axiom.infra.prompt_composer import PromptComposer

    def _turn(retrieved: str):
        c = PromptComposer()
        c.add("identity", name="persona", content="You are careful.", source="test")
        c.add("retrieved", name="chunks", content=retrieved, source="rag")
        return c.observability_payload()["stack_id"]

    assert _turn("soil report A") == _turn("irradiation log B")


def test_two_turns_with_a_different_persona_do_not_share_a_stack_id():
    from axiom.infra.prompt_composer import PromptComposer

    def _turn(persona: str):
        c = PromptComposer()
        c.add("identity", name="persona", content=persona, source="test")
        return c.observability_payload()["stack_id"]

    assert _turn("You are careful.") != _turn("You are reckless.")
