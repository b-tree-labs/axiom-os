# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Importing a written procedure as a capability.

ADR-063 renders a SkillSpec to a SKILL.md. Nothing read one back, so a project
arriving with a directory of hand-written skill documents — the common shape
now — got nothing from the platform. Its skills were quotable, not runnable.
"""

from __future__ import annotations

import pytest

from axiom.infra.skill_md import (
    SkillDocumentError,
    read_skill_md,
    register_skill_md_dir,
    spec_from_document,
)
from axiom.infra.skills import SkillRegistry, SkillSpec
from axiom.infra.skills_emit import _render_skill_md

HAND_WRITTEN = """\
---
name: review-a-deck
description: Check a generated input deck before anybody runs it.
allowed-tools: [corral.spec_check, data.validate]
inputs:
  mesh: str
  strict: bool
---

1. Read the spec document and confirm its format tag.
2. Run corral.spec_check and stop on any error.
3. Only then render.
"""


class TestRoundTrip:
    """What the platform emits, it must be able to read back.

    Two representations of one thing drift, and the drift shows up as a
    capability whose document says something its registration does not.
    """

    def _original(self):
        return SkillSpec(
            name="data.conform_try",
            fn=lambda p, c: None,
            description="Dry-run a normalizer over one record.",
            long_description="The loop a normalizer author wants.",
            inputs={"schema_ref": "str", "record": "dict"},
            allowed_tools=("data.validate",),
        )

    @pytest.mark.parametrize(
        "attribute", ["name", "description", "inputs", "allowed_tools"]
    )
    def test_every_declared_field_survives(self, attribute):
        original = self._original()
        back = spec_from_document(read_skill_md(_render_skill_md(original, "1.2.0")))

        assert getattr(back, attribute) == getattr(original, attribute)

    def test_the_body_becomes_the_long_description(self):
        original = self._original()
        back = spec_from_document(read_skill_md(_render_skill_md(original, "1.2.0")))

        assert original.long_description in back.long_description


class TestAHandWrittenDocument:
    def test_it_parses(self):
        doc = read_skill_md(HAND_WRITTEN)

        assert doc.name == "review-a-deck"
        assert doc.description.startswith("Check a generated input deck")

    def test_a_plain_mapping_of_inputs_is_accepted(self):
        """Emitted as a list of {name, type}; written by hand as a mapping.
        Refusing the second would reject most of what people actually have."""
        assert read_skill_md(HAND_WRITTEN).inputs == {"mesh": "str", "strict": "bool"}

    def test_allowed_tools_are_carried_through(self):
        """SkillSpec.allowed_tools is empty in every declaration in this repo.
        An imported document gives the field its first live consumer."""
        doc = read_skill_md(HAND_WRITTEN)

        assert doc.allowed_tools == ("corral.spec_check", "data.validate")

    def test_the_procedure_is_what_invoking_it_returns(self):
        """A skill document has no code. The useful thing to hand a caller is
        the instructions plus the tools they may use while following them."""
        spec = spec_from_document(read_skill_md(HAND_WRITTEN))

        result = spec.fn({}, None)

        assert result.ok
        assert "Run corral.spec_check" in result.value["instructions"]
        assert result.value["allowed_tools"] == ["corral.spec_check", "data.validate"]

    def test_reading_a_procedure_has_no_side_effects(self):
        """What the procedure then tells an agent to do is gated where that
        happens, by the capabilities it calls."""
        spec = spec_from_document(read_skill_md(HAND_WRITTEN))

        assert spec.side_effects is False
        assert spec.idempotent is True


class TestWhatItGains:
    def test_an_imported_document_reaches_every_surface(self):
        """The benefit: a file a harness had to be handed becomes addressable."""
        spec = spec_from_document(read_skill_md(HAND_WRITTEN))

        assert set(spec.surfaces) == {"cli", "mcp", "agent_tool", "skill_md"}

    def test_a_namespace_can_be_applied_to_an_unqualified_name(self):
        spec = spec_from_document(read_skill_md(HAND_WRITTEN), namespace="moose")

        assert spec.name == "moose.review-a-deck"

    def test_an_already_qualified_name_is_left_alone(self):
        doc = read_skill_md(HAND_WRITTEN.replace("name: review-a-deck", "name: x.y"))

        assert spec_from_document(doc, namespace="moose").name == "x.y"


class TestARefusalRatherThanASkip:
    """A directory is imported by somebody who believes every file will
    register. Dropping one quietly leaves a capability that does not exist and
    no reason to look."""

    def test_a_document_with_no_frontmatter(self):
        with pytest.raises(SkillDocumentError, match="no YAML frontmatter"):
            read_skill_md("Just some prose about how to review a deck.")

    def test_a_document_with_no_name(self):
        with pytest.raises(SkillDocumentError, match="declares no 'name'"):
            read_skill_md("---\ndescription: something\n---\n\nBody.\n")

    def test_unknown_frontmatter_keys_are_tolerated(self):
        """The format belongs to other tools too, and they will add fields."""
        doc = read_skill_md(
            "---\nname: x\nlicense: MIT\nmodel: opus\n---\n\nBody.\n"
        )

        assert doc.name == "x"


class TestImportingADirectory:
    def _write(self, root, layout):
        if layout == "nested":
            (root / "review-a-deck").mkdir()
            (root / "review-a-deck" / "SKILL.md").write_text(HAND_WRITTEN)
        else:
            (root / "review-a-deck.md").write_text(HAND_WRITTEN)

    @pytest.mark.parametrize("layout", ["nested", "flat"])
    def test_both_layouts_people_use(self, tmp_path, layout):
        self._write(tmp_path, layout)
        registry = SkillRegistry()

        assert register_skill_md_dir(registry, tmp_path, namespace="moose") == [
            "moose.review-a-deck"
        ]
        assert registry.spec("moose.review-a-deck") is not None

    def test_a_bad_document_stops_the_import(self, tmp_path):
        self._write(tmp_path, "flat")
        (tmp_path / "notes.md").write_text("Just prose, no frontmatter.\n")

        with pytest.raises(SkillDocumentError):
            register_skill_md_dir(SkillRegistry(), tmp_path, namespace="moose")

    def test_a_missing_directory_says_so(self, tmp_path):
        with pytest.raises(SkillDocumentError, match="not a directory"):
            register_skill_md_dir(SkillRegistry(), tmp_path / "nope")
