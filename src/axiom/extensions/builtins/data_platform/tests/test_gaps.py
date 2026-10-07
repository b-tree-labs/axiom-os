# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A steward cannot declare what they cannot see.

Every gap found on the live install this week was found because a person went
looking, which is not a process. The detection existed and was scattered
across four modules and a query somebody ran once.
"""

from __future__ import annotations

from ..gaps import (
    NEEDS_DECLARATION,
    NEEDS_PRODUCER,
    NEEDS_RETRACTION,
    assess,
    report,
)


class TestNothingToDeclareSaysSo:
    def test_a_clean_site_gets_a_sentence_not_an_empty_list(self):
        assert assess("site-a") == []
        assert "nothing to declare" in report("site-a", [])[0]


class TestOrderedByRowsNotBySeverity:
    """A severity is a judgement made on a site's behalf, and it is the wrong
    judgement often enough to be worse than none. 19.1 million unitless rows
    and three unitless rows are the same defect and nowhere near the same
    problem."""

    def test_the_biggest_comes_first(self):
        gaps = assess(
            "s",
            unitless={"a": 10},
            roleless={"b": 1_000_000},
        )
        assert [g.kind for g in gaps] == ["no role", "no unit"]

    def test_and_flips_when_the_counts_do(self):
        gaps = assess("s", unitless={"a": 1_000_000}, roleless={"b": 10})
        assert [g.kind for g in gaps] == ["no unit", "no role"]

    def test_the_order_is_stable_on_a_tie(self):
        a = assess("s", unitless={"x": 5}, roleless={"y": 5})
        b = assess("s", roleless={"y": 5}, unitless={"x": 5})
        assert [g.kind for g in a] == [g.kind for g in b]


class TestAStewardSListIsThingsAStewardCanDo:
    def test_a_unit_is_declarable(self):
        gap = assess("s", unitless={"a": 1})[0]
        assert gap.needs == NEEDS_DECLARATION
        assert gap.actionable_by_steward

    def test_a_timestamp_collision_is_the_producer_s(self):
        """No declaration closes it. Saying otherwise sends a steward to edit
        a file that cannot help."""
        gap = assess("s", colliding_instants=10, colliding_divergent=10)[0]
        assert gap.needs == NEEDS_PRODUCER
        assert not gap.actionable_by_steward

    def test_a_constant_channel_is_a_RETRACTION_not_a_correction(self):
        """ADR-042 D10: a correction says the value was wrong; a retraction
        says stop deriving from this while the record persists. Nothing about
        a never-varying channel's 696,231 rows is incorrect, so declaring a
        replacement value would assert something false about every one."""
        gap = assess("s", constant={"a": 5})[0]
        assert gap.needs == NEEDS_RETRACTION
        assert not gap.actionable_by_steward
        assert "not a correction" in gap.summary

    def test_and_it_names_the_three_operations_rather_than_bundling_them(self):
        """Retract, reclassify, or fix the source are different acts, and
        bundling them behind one word is how a steward asserts something
        false."""
        gap = assess("s", constant={"a": 5})[0]
        for act in ("RETRACT", "RECLASSIFY", "fix the source"):
            assert act in gap.fix

    def test_the_report_counts_what_is_actually_actionable(self):
        gaps = assess("s", unitless={"a": 5}, colliding_instants=9, colliding_divergent=9)
        text = "\n".join(report("s", gaps))
        assert "1 of 2 can be closed by declaring something" in text


class TestEachGapNamesTheSpecificAct:
    def test_a_unit_gap_names_the_rederive_that_applies_it(self):
        """Declaring without re-deriving fixes the future and leaves the
        history, which is the state this was all stuck in."""
        gap = assess("site-a", unitless={"a": 1})[0]
        assert "rederive --site site-a" in gap.fix

    def test_a_role_gap_warns_against_inventing_a_name(self):
        """A role nobody else uses joins nothing, which is the failure it was
        meant to fix."""
        gap = assess("s", roleless={"a": 1})[0]
        assert "Reuse an existing role" in gap.fix

    def test_the_collision_gap_says_do_not_deduplicate(self):
        """Zero of 370,889 collisions on the worst channel were re-sends."""
        gap = assess("s", colliding_instants=1, colliding_divergent=1)[0]
        assert "Do NOT deduplicate" in gap.fix

    def test_an_unmapped_ref_is_named_as_upstream_of_the_rest(self):
        gap = assess("s", unmapped_refs={"a/v1": 100})[0]
        assert "upstream of every other gap" in gap.summary


class TestTheDetailGivesSomewhereToStart:
    def test_the_worst_channels_are_named(self):
        gap = assess("s", unitless={"big": 900, "small": 1})[0]
        assert gap.detail[0].startswith("big")

    def test_a_long_list_is_truncated_with_a_count(self):
        """A steward scrolling 119 names has not been helped."""
        gap = assess("s", roleless={f"ch{i}": 10 for i in range(119)})[0]
        assert gap.detail[-1] == "and 113 more"

    def test_counts_are_formatted_for_reading(self):
        gap = assess("s", unitless={"a": 1093655})[0]
        assert "1,093,655" in gap.detail[0]


class TestTheRealSiteShape:
    """Measured on a live install."""

    def test_roles_outrank_units_there(self):
        gaps = assess(
            "site-a",
            unitless={f"c{i}": 127000 for i in range(27)},
            roleless={f"c{i}": 146000 for i in range(119)},
            colliding_instants=2_427_680, colliding_divergent=2_427_591,
            fault_values={"dt01": 16898},
        )
        assert gaps[0].kind == "no role"
        assert gaps[0].rows > gaps[1].rows

    def test_three_of_the_four_are_declarable(self):
        gaps = assess(
            "site-a",
            unitless={"a": 1}, roleless={"b": 2},
            fault_values={"c": 3}, colliding_instants=4, colliding_divergent=4,
        )
        assert sum(1 for g in gaps if g.actionable_by_steward) == 3


class TestAProposalWithoutABlastRadiusReadsAsSafe:
    """Every gap says what closing it would DO, in rows and direction.
    "Withholds 46,662 readings" and "adds a unit to 3,430,514 rows" are
    opposite kinds of change wearing the same word."""

    def test_a_unit_declaration_changes_no_value(self):
        gap = assess("s", unitless={"a": 100})[0]
        assert "adds a unit to 100 row(s)" in gap.effect
        assert "no value changes" in gap.effect.lower()

    def test_a_fault_declaration_withholds_and_says_so_loudly(self):
        """This one is the opposite direction: rows currently served stop
        being served, and every chart over them changes."""
        gap = assess("s", fault_values={"a": 46662})[0]
        assert "WITHHOLDS 46,662" in gap.effect
        assert "will change" in gap.effect

    def test_a_retraction_changes_no_value_either(self):
        gap = assess("s", constant={"a": 5})[0]
        assert "changes no value" in gap.effect
        assert "remain for audit" in gap.effect

    def test_the_collision_gap_says_a_dedupe_would_delete(self):
        gap = assess("s", colliding_instants=9, colliding_divergent=9)[0]
        assert "DELETE real" in gap.effect

    def test_every_gap_carries_one(self):
        gaps = assess(
            "s", unitless={"a": 1}, roleless={"b": 1}, fault_values={"c": 1},
            constant={"d": 1}, unmapped_refs={"e/v1": 1},
            colliding_instants=1, colliding_divergent=1,
        )
        assert all(g.effect for g in gaps)

    def test_the_report_shows_it(self):
        text = "\n".join(report("s", assess("s", fault_values={"a": 5})))
        assert "effect:" in text


# ----------------------------------------------------------------- the skill


class TestTheSkillIsReachable:
    def test_it_is_a_cli_verb(self):
        from ..skills import verbs

        assert "gaps" in verbs()

    def test_the_site_is_required_by_the_parser(self):
        import pytest

        from ..cli import _parser

        with pytest.raises(SystemExit):
            _parser().parse_args(["gaps"])

    def test_collisions_are_off_unless_asked(self):
        """That query groups every row the site has, which is a
        maintenance-shaped cost on a site with tens of millions."""
        from ..cli import _parser

        assert _parser().parse_args(["gaps", "--site", "s"]).collisions is False
        assert _parser().parse_args(["gaps", "--site", "s", "--collisions"]).collisions

    def test_it_registers_with_a_spec_and_is_readable_from_mcp(self):
        from axiom.infra.skills import SkillRegistry

        from ..skills import bind

        registry = SkillRegistry()
        bind(registry)
        spec = registry.spec("data.gaps")
        assert spec.description
        assert "mcp" in spec.surfaces
        assert spec.side_effects is False

    def test_no_site_is_refused_before_a_connection_is_opened(self, tmp_path):
        import logging

        from axiom.infra.skills import SkillContext, SkillRegistry

        from ..skills import gaps as skill

        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=str(tmp_path),
            logger=logging.getLogger(__name__),
        )
        out = skill.run({"site": "  "}, ctx)
        assert out.ok is False
        assert "missing required param: site" in out.errors[0]

    def test_it_declares_the_tier_it_reads(self):
        """ADR-128 E5: a diagnostic may read a working tier provided it says
        which one."""
        from ..skills.gaps import TIER_READ

        assert TIER_READ == "silver"

    def test_a_skipped_collision_check_is_said_rather_than_omitted(self):
        """A list missing a check reads as a list with nothing to report on
        it."""
        import inspect

        from ..skills import gaps as skill

        source = inspect.getsource(skill.run)
        assert "not checked" in source
