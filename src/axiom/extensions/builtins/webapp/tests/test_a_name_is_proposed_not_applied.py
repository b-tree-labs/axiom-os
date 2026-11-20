# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A suggested name is a proposal. Nothing writes one on its own."""

from __future__ import annotations

from axiom.extensions.builtins.webapp.views.labels import default_labels
from axiom.extensions.builtins.webapp.views.suggest import (
    SUGGEST_PROMPT,
    parse_proposals,
    prompt_for,
)

ASKED = ["NCDT1:HEAT:TC-CP1_1", "NCDT1:GAS:PT31"]


class TestTheDefaultDropsOnlyWhatIsRedundant:
    def test_a_hierarchical_name_is_left_alone(self):
        """`HEAT` tells a reader which part of the loop they are looking at.
        A shorter name that is harder to read is not an improvement."""
        assert default_labels([("NCDT1:HEAT:TC-CP1_1", "degC")]) == {
            "NCDT1:HEAT:TC-CP1_1": "NCDT1:HEAT:TC-CP1_1"
        }

    def test_but_a_unit_the_axis_already_carries_goes(self):
        """`corrected_cm` beside an axis labelled cm says it twice, and the
        axis is still there."""
        assert default_labels([("corrected_cm", "cm")]) == {"corrected_cm": "corrected"}

    def test_and_a_unit_only_the_NAME_carries_stays(self):
        """It is the one clue the channel gives. Dropping it hides it."""
        assert default_labels([("measured_cm", "")]) == {"measured_cm": "measured_cm"}

    def test_two_that_would_collide_both_keep_their_names(self):
        got = default_labels([("a_cm", "cm"), ("a", "cm")])
        assert got == {"a_cm": "a_cm", "a": "a"}


class TestWhatTheModelIsTold:
    def test_the_channel_its_unit_and_its_feed(self):
        asked = prompt_for([{"channel": "PT31", "unit": "psi", "feed": "senna.epics"}])
        assert "PT31" in asked and "psi" in asked and "senna.epics" in asked

    def test_and_the_map_s_own_words_when_there_are_any(self):
        """The best evidence there is, and it costs nothing to pass on."""
        asked = prompt_for([{"channel": "PT31", "description": "gas skid pressure"}])
        assert "gas skid pressure" in asked

    def test_never_readings(self):
        """Naming a channel is a question about the channel. Sending values
        would be sending data to answer a question about a label."""
        asked = prompt_for([{"channel": "PT31", "unit": "psi", "points": [[1, 2.0]]}])
        assert "2.0" not in asked

    def test_leaving_a_name_alone_is_asked_for_explicitly(self):
        assert "UNCHANGED" in SUGGEST_PROMPT


class TestWhatComesBack:
    def test_a_well_formed_answer_becomes_proposals(self):
        got = parse_proposals(
            '[{"channel": "NCDT1:GAS:PT31", "label": "Gas skid pressure", "why": "expanded"}]',
            ASKED,
        )
        assert [(p.channel, p.label) for p in got] == [
            ("NCDT1:GAS:PT31", "Gas skid pressure")
        ]

    def test_fenced_json_is_read_anyway(self):
        """Models fence about half the time; taking the bracketed run is more
        reliable than asking them not to."""
        got = parse_proposals(
            'Sure!\n```json\n[{"channel": "NCDT1:GAS:PT31", "label": "PT31"}]\n```',
            ASKED,
        )
        assert len(got) == 1

    def test_an_unchanged_name_is_marked_as_such(self):
        got = parse_proposals('[{"channel": "NCDT1:GAS:PT31", "label": "NCDT1:GAS:PT31"}]', ASKED)
        assert got[0].unchanged is True

    def test_a_channel_nobody_asked_about_is_dropped(self):
        got = parse_proposals('[{"channel": "SOMETHING:ELSE", "label": "Nice"}]', ASKED)
        assert got == []

    def test_so_is_a_duplicate(self):
        got = parse_proposals(
            '[{"channel": "NCDT1:GAS:PT31", "label": "A"},'
            ' {"channel": "NCDT1:GAS:PT31", "label": "B"}]',
            ASKED,
        )
        assert [p.label for p in got] == ["A"]

    def test_and_prose_produces_nothing_rather_than_a_bad_name(self):
        """An empty answer costs a click. A wrong one costs trust."""
        assert parse_proposals("I think PT31 means pressure transmitter 31.", ASKED) == []

    def test_an_absurdly_long_label_is_refused(self):
        got = parse_proposals(
            f'[{{"channel": "NCDT1:GAS:PT31", "label": "{"x" * 80}"}}]', ASKED
        )
        assert got == []

    def test_an_empty_label_is_not_a_proposal(self):
        assert parse_proposals('[{"channel": "NCDT1:GAS:PT31", "label": "  "}]', ASKED) == []
