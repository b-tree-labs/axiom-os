# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The "when" is derived, never authored.

A hand-written trigger per capability is the staleness trap SKILL.md just spent a
whole program escaping: a second place to keep in sync, drifting within a
quarter. So the situation line is computed from what a SkillSpec already carries,
and the SELECTION of which capabilities appear is computed from what this install
has versus what it has actually used.

That second half is the self-improving part. The block shows the discovery GAP.
Use a capability and it drops out; an empty block means discovery succeeded,
which is a measurable end state rather than a matter of opinion. No model is
involved in either half — same inputs, same block, always.
"""

from __future__ import annotations

from axiom.memory.rendering import capabilities_for_block, when_for


class _Spec:
    def __init__(self, name, description="", side_effects=None):
        self.name = name
        self.description = description
        self.side_effects = side_effects


def test_a_read_capability_frames_as_a_question():
    w = when_for(_Spec("data.gold_aggregate", "the mean of a column over a window", side_effects=False))
    assert w.startswith("someone asks")
    assert "mean of a column over a window" in w


def test_a_writing_capability_frames_as_an_intent():
    """A capability that changes something is reached for from a different kind
    of sentence, and saying so is what stops an assistant offering a mutation
    when someone only asked a question."""
    w = when_for(_Spec("press.publish", "publish a release note", side_effects=True))
    assert w.startswith("someone wants to")
    assert "publish a release note" in w


def test_an_undeclared_side_effect_is_treated_as_writing():
    """Conservative on purpose: the projector already treats an undeclared
    side_effect as a write, and the discovery block must not disagree with the
    gate about what a capability does."""
    assert when_for(_Spec("x.y", "do a thing", side_effects=None)).startswith("someone wants to")


def test_a_capability_with_no_description_yields_no_trigger():
    """Rather than emit "when someone asks about x.y" — a line that teaches an
    assistant nothing and spends budget doing it."""
    assert when_for(_Spec("x.y", "", side_effects=False)) == ""


def test_derivation_is_deterministic():
    spec = _Spec("data.gold_series", "a bucketed series", side_effects=False)
    first = when_for(spec)
    for _ in range(5):
        assert when_for(spec) == first


# --- selection: the block shows what has NOT been reached for ---------------


def _installed():
    return [
        _Spec("data.gold_aggregate", "a total or average over stored data", side_effects=False),
        _Spec("federation.node_status", "what is deployed on a node", side_effects=False),
        _Spec("memory.search", "a decision from an earlier session", side_effects=False),
    ]


def test_the_block_shows_capabilities_that_have_never_been_used():
    out = capabilities_for_block(_installed(), used=set())
    assert {c["name"] for c in out} == {
        "data.gold_aggregate", "federation.node_status", "memory.search",
    }


def test_using_a_capability_drops_it_out_of_the_block():
    """The self-improving half. Discovery succeeded for that one, so it stops
    spending context on every session."""
    out = capabilities_for_block(_installed(), used={"federation.node_status"})
    assert "federation.node_status" not in {c["name"] for c in out}
    assert len(out) == 2


def test_an_install_whose_capabilities_are_all_used_renders_nothing():
    """The end state worth naming: no gap left to advertise."""
    assert capabilities_for_block(_installed(), used={
        "data.gold_aggregate", "federation.node_status", "memory.search",
    }) == []


def test_selection_is_deterministic_and_ordered():
    """Same install, same usage, same block — otherwise the write-back rewrites
    thirteen files for nothing."""
    first = capabilities_for_block(_installed(), used={"memory.search"})
    for _ in range(5):
        assert capabilities_for_block(_installed(), used={"memory.search"}) == first


def test_negative_control_selection_can_return_nothing_for_the_right_reason():
    # An empty install is empty for a different reason than a fully-used one;
    # both render nothing, and neither should raise.
    assert capabilities_for_block([], used=set()) == []


# --- found by rendering the REAL capability set, not short fixtures ---------


_REAL = (
    "Dry-run a registered normalizer over one bronze record and report the "
    "canonical rows it yields, plus the mistakes silver would absorb silently — "
    "a duplicate channel, a naive timestamp, a missing unit. Writes nothing. "
    "Omit schema_ref to list what is registered"
)


def test_a_long_description_is_cut_to_its_first_sentence():
    """Real descriptions run to several sentences. Rendered whole, one block
    line reached 300+ characters — the budget bounded LINES while the block
    ballooned, which is the same failure the budget exists to prevent."""
    w = when_for(_Spec("data.conform_try", _REAL, side_effects=False))
    assert len(w) < 160, f"trigger ran to {len(w)} chars"
    assert "Writes nothing" not in w, "kept a second sentence"
    assert "dry-run a registered normalizer" in w.lower()


def test_the_trigger_reads_as_one_sentence():
    """"When someone asks about Deterministic math over a series" reads as two
    fragments bolted together. The description's leading capital is lowered so
    the line is a sentence somebody would actually write."""
    w = when_for(_Spec("analytics.compute", "Deterministic math over a numeric series", side_effects=False))
    assert w == "someone asks about deterministic math over a numeric series"


def test_an_acronym_keeps_its_case():
    """Lowering blindly would turn RAG into rag and MCP into mcp."""
    w = when_for(_Spec("rag.retrieve", "RAG retrieval over the indexed corpus", side_effects=False))
    assert w.startswith("someone asks about RAG retrieval")
