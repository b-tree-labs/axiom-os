# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi note list` must not save a note that says "list".

`note` takes free text positionally, so a subcommand-shaped word became
content: `axi note list` silently wrote a note whose entire body was "list"
and left a file behind. A read-shaped verb that writes is the wrong way round
— of the two readings, capturing is the destructive one.
"""
from __future__ import annotations

import argparse

import pytest

from axiom.extensions.builtins.note import cli


@pytest.fixture
def notes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_notes_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_today_file", lambda: tmp_path / "today.md")
    return tmp_path


def _run(text, *, literal=False, list_flag=False):
    return argparse.Namespace(text=text, list=list_flag, literal=literal)


class TestAListingWordIsNotANote:
    @pytest.mark.parametrize("word", ["list", "ls", "recent", "LIST"])
    def test_it_lists_instead_of_writing(self, word, notes, capsys):
        cli.cmd_note(_run([word]))
        assert not (notes / "today.md").exists(), (
            f"`note {word}` wrote a note instead of listing"
        )
        assert "not as a note" in capsys.readouterr().out

    def test_a_real_note_is_still_captured(self, notes, capsys):
        cli.cmd_note(_run(["something", "worth", "keeping"]))
        assert (notes / "today.md").exists()
        assert "worth keeping" in (notes / "today.md").read_text()

    def test_a_longer_note_starting_with_the_word_is_captured(self, notes):
        """Only a BARE listing word is ambiguous. "list the reactor states"
        is plainly a note."""
        cli.cmd_note(_run(["list", "the", "reactor", "states"]))
        assert "list the reactor states" in (notes / "today.md").read_text()

    def test_the_escape_hatch_the_message_advertises_actually_works(self, notes):
        """argparse consumes `--`, so the flag has to be read from the raw
        argv. Advertising an escape hatch that does nothing is worse than
        offering none."""
        cli.cmd_note(_run(["list"], literal=True))
        assert (notes / "today.md").read_text().strip().endswith("list")


class TestTheRawArgvIsWhatDecides:
    def test_main_sets_literal_from_a_double_dash(self, notes, monkeypatch):
        captured = {}
        monkeypatch.setattr(cli, "cmd_note", lambda a: captured.update(literal=a.literal))
        cli.main(["--", "list"])
        assert captured["literal"] is True

    def test_main_leaves_literal_off_without_one(self, notes, monkeypatch):
        captured = {}
        monkeypatch.setattr(cli, "cmd_note", lambda a: captured.update(literal=a.literal))
        cli.main(["list"])
        assert captured["literal"] is False
