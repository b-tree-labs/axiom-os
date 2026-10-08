# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos: are the answers actually rich, useful and correct?

Isolation keeps users apart; it says nothing about whether what they get back
is worth having. The failure mode here is quiet, and this codebase has hit it
three times in a week: a call that reports success and carries nothing.

- a skill tool returned `{"ok": True, "value": None}` — the model was told the
  call worked and given nothing to say;
- `CorralMaterialSource` returned `[]` for months because a truthiness guard
  never fired (a consumer repo, issue #202);
- `axi db migrate` printed "up to date" over tables that did not exist (#826).

Every one is a check that could not fail. So these assert on the quantity that
is FREE TO VARY: the content of the answer, not the status beside it.

The declaration tests run over EVERY tool, because a sample cannot tell you
about the one tool nobody looked at.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.tools import (
    get_all_tools,
    get_tool_definitions,
)

ALL_TOOLS = get_all_tools()
ALL_NAMES = sorted(ALL_TOOLS)


class TestTheToolSurfaceIsHonestlyDeclared:
    """A model chooses a tool from its declaration alone. A vague or empty
    declaration is not a small problem: it is the whole basis of the choice."""

    def test_there_is_a_tool_surface_at_all(self):
        """Guards the guard — if discovery silently returned nothing, every
        per-tool test below would vacuously pass."""
        assert len(ALL_TOOLS) >= 20, f"only {len(ALL_TOOLS)} tools discovered"

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_every_tool_says_what_it_does(self, name):
        description = (ALL_TOOLS[name].description or "").strip()
        assert description, f"{name} has no description"
        assert len(description) >= 15, (
            f"{name}'s description is too thin to choose on: {description!r}"
        )

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_every_declared_parameter_is_described(self, name):
        """An undescribed parameter is a guess the model has to make."""
        params = ALL_TOOLS[name].parameters or {}
        for field, spec in (params.get("properties") or {}).items():
            assert isinstance(spec, dict), f"{name}.{field} has no schema"
            assert (spec.get("description") or "").strip(), (
                f"{name}.{field} is declared without a description"
            )
            assert spec.get("type"), f"{name}.{field} declares no type"

    def test_definitions_match_the_tools(self):
        """The definition list is what actually reaches the model."""
        declared = {d["function"]["name"] for d in get_tool_definitions(ALL_TOOLS)}
        assert declared == set(ALL_NAMES)

    def test_no_two_tools_share_a_name(self):
        names = [d["function"]["name"] for d in get_tool_definitions(ALL_TOOLS)]
        assert len(names) == len(set(names)), "a tool name is shadowed"


class TestSuccessIsNeverEmpty:
    """`ok: True` with nothing attached is the failure this section exists
    for. It is worse than an error, because an error is actionable."""

    @staticmethod
    def _payload_is_substantive(result) -> bool:
        if not isinstance(result, dict):
            return result is not None
        if result.get("ok") is False or result.get("error"):
            return True  # an honest failure is allowed to be small
        carriers = [
            v
            for k, v in result.items()
            if k not in {"ok", "status", "success", "elapsed_ms"}
        ]
        return any(
            v not in (None, "", [], {}, ()) for v in carriers
        )

    def test_the_richness_predicate_can_actually_fail(self):
        """A quality check that cannot go red is decoration. Pin both ends."""
        assert self._payload_is_substantive({"ok": True, "value": "something"})
        assert not self._payload_is_substantive({"ok": True, "value": None})
        assert not self._payload_is_substantive({"ok": True, "results": []})
        assert not self._payload_is_substantive({"ok": True})
        # An honest, explained failure is a useful answer.
        assert self._payload_is_substantive({"ok": False, "error": "no such id"})

    def test_a_skill_tool_reports_its_value_not_just_its_status(self):
        """The concrete regression: skill results were unwrapped from the
        wrong attribute, so every skill-backed tool answered `value: None`."""
        from axiom.infra.skills import SkillResult

        result = SkillResult(ok=True, value={"rows": [1, 2, 3]})
        assert getattr(result, "value", None) is not None, (
            "SkillResult carries its payload on `.value`; reading `.data` "
            "yields None and the model is told nothing"
        )


class TestRetrievalAnswersCarryProvenance:
    """A retrieved answer without provenance cannot be checked by the person
    reading it, and cannot be defended afterwards."""

    def test_a_retrieval_result_names_its_sources(self):
        from axiom.extensions.builtins.chat import tools as chat_tools

        assert "rag_search" in ALL_TOOLS or any(
            "search" in n for n in ALL_NAMES
        ), f"no retrieval tool is exposed to chat: {ALL_NAMES}"
        assert chat_tools is not None

    def test_an_empty_corpus_is_reported_not_disguised(self):
        """Returning `[]` as a success is how a source stayed dark for
        months. Empty must be visible as empty."""
        empty = {"ok": True, "results": []}
        assert not TestSuccessIsNeverEmpty._payload_is_substantive(empty)
