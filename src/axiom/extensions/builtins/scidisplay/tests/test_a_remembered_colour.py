# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
""""Always draw this channel in blue", and it keeps.

A derived colour is the same answer every time, which is most of what a code
needs. What it cannot do is agree with a person who has already decided.
Someone who has read one channel off a blue line for ten years should be able
to say so once.

So it is a file, not a session, and the tests here are about the ways a file
goes wrong: a half-written one, a typo in a hand edit, a channel name with a
colon in it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay import colour_preferences as prefs
from axiom.extensions.builtins.scidisplay.chart_colour import (
    assign_colours,
    derived_colour,
    normalise_colour,
)


@pytest.fixture(autouse=True)
def _own_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    return tmp_path


class TestItKeeps:
    def test_nothing_remembered_is_not_a_failure(self):
        """Nobody has expressed a preference yet, which is a normal state."""
        assert prefs.load() == {}

    def test_what_was_said_comes_back(self):
        prefs.remember("Power", "blue")
        assert prefs.load() == {"Power": normalise_colour("blue")}

    def test_across_processes(self, tmp_path):
        prefs.remember("Power", "blue")
        assert prefs.path().exists()
        assert "Power" in prefs.path().read_text(encoding="utf-8")

    def test_a_name_is_stored_as_its_hex(self):
        """So the file says what will actually be drawn, and a colour nobody
        can resolve is refused here rather than at the next chart."""
        prefs.remember("Power", "blue")
        assert "#0072b2" in prefs.path().read_text(encoding="utf-8")

    def test_saying_it_again_replaces_it(self):
        prefs.remember("Power", "blue")
        prefs.remember("Power", "green")
        assert prefs.load()["Power"] == normalise_colour("green")

    def test_forgetting_says_whether_it_did(self):
        prefs.remember("Power", "blue")
        assert prefs.forget("Power") is True
        assert prefs.forget("Power") is False
        assert prefs.load() == {}

    def test_a_channel_name_with_punctuation_survives(self):
        """Channel names come from somebody else's instrument and routinely
        carry dots and colons, which a bare TOML key cannot."""
        name = "NCDT1:HEAT:TC-CP1_1.raw"
        prefs.remember(name, "#123456")
        assert prefs.load() == {name: "#123456"}


class TestWhatIsRefused:
    def test_a_colour_nobody_can_name(self):
        with pytest.raises(ValueError, match="chartreuse"):
            prefs.remember("Power", "chartreuse")

    def test_and_nothing_is_written_when_it_is(self):
        with pytest.raises(ValueError):
            prefs.remember("Power", "chartreuse")
        assert not prefs.path().exists()

    def test_a_channel_with_no_name(self):
        with pytest.raises(prefs.PreferencesError, match="no name"):
            prefs.remember("   ", "blue")


class TestAFileThatCannotBeRead:
    """An unreadable preferences file is an ERROR, not an empty dictionary.

    Drawing in the derived colours while a person's own settings sit unread is
    the quiet kind of wrong: the figure looks fine and it is not the figure
    they asked for.
    """

    def test_malformed_toml_is_reported(self):
        prefs.path().write_text("this is not toml = = =", encoding="utf-8")
        with pytest.raises(prefs.PreferencesError, match="could not be read"):
            prefs.load()

    def test_a_typo_in_a_hand_edit_is_reported_with_the_channel(self):
        prefs.path().write_text('[colours]\n"Power" = "bleu"\n', encoding="utf-8")
        with pytest.raises(prefs.PreferencesError, match="Power"):
            prefs.load()

    def test_the_section_being_the_wrong_shape_is_reported(self):
        prefs.path().write_text('colours = "blue"\n', encoding="utf-8")
        with pytest.raises(prefs.PreferencesError, match="table"):
            prefs.load()

    def test_an_interrupted_write_leaves_nothing_half_done(self):
        prefs.remember("Power", "blue")
        leftovers = list(prefs.path().parent.glob("*.writing"))
        assert leftovers == []


class TestItReachesTheFigure:
    def test_a_remembered_colour_beats_the_derived_one(self):
        prefs.remember("Power", "green")
        colours, _ = assign_colours([("Power", "W")], preferred=prefs.load())
        assert colours[0] == normalise_colour("green")
        assert colours[0] != derived_colour("Power", "W")

    def test_and_the_document_beats_the_remembered_one(self):
        """A document's colours travel with it; this file never leaves the
        machine it is on."""
        prefs.remember("Power", "green")
        colours, _ = assign_colours(
            [("Power", "W")], declared={"Power": "red"}, preferred=prefs.load()
        )
        assert colours[0] == normalise_colour("red")

    def test_a_pinned_channel_does_not_move_for_its_neighbours(self):
        prefs.remember("Power", "green")
        alone, _ = assign_colours([("Power", "W")], preferred=prefs.load())
        crowd, _ = assign_colours(
            [("Power", "W"), ("P2", "W"), ("P3", "W")], preferred=prefs.load()
        )
        assert crowd[0] == alone[0] == normalise_colour("green")
