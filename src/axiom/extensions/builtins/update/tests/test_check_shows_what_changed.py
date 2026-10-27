# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`update --check` says what changed, not only which wheels would move.

The startup banner tells a user to run `<cli> update --check` "for
details". It answered with a dependency dry-run — the packages that would
be installed, and nothing about why anyone would want them.

`fetch_release_notes` already existed and was reached from exactly one
place, `chat/entry.py`. So the only person who ever saw a changelog was
one who happened to start a chat session; somebody who runs `daq` or
`telemetry` verbs all day saw the nudge and never the reason.
"""

from __future__ import annotations

import types
from unittest.mock import patch

from axiom.extensions.builtins.update import cli as ucli

NEWER = types.SimpleNamespace(
    is_newer=True, current="1.13.0", available="1.14.0", repo="owner/repo"
)
CURRENT = types.SimpleNamespace(
    is_newer=False, current="1.14.0", available="1.14.0", repo="owner/repo"
)

_CHECK = "axiom.extensions.builtins.update.version_check.VersionChecker.check_remote_version"
_NOTES = "axiom.extensions.builtins.update.release_notes.fetch_release_notes"


class TestItShowsTheNotes:
    def test_the_changelog_reaches_the_terminal(self, capsys):
        with patch(_CHECK, return_value=NEWER), patch(_NOTES, return_value="- a real change"):
            ucli._print_release_notes()
        assert "a real change" in capsys.readouterr().out

    def test_both_versions_are_named(self, capsys):
        with patch(_CHECK, return_value=NEWER), patch(_NOTES, return_value="- x"):
            ucli._print_release_notes()
        out = capsys.readouterr().out
        assert "1.13.0" in out and "1.14.0" in out


class TestItStaysQuietWhenItShould:
    def test_nothing_is_printed_when_already_current(self, capsys):
        with patch(_CHECK, return_value=CURRENT):
            ucli._print_release_notes()
        assert capsys.readouterr().out == ""

    def test_a_failed_fetch_does_not_stop_the_check(self, capsys):
        """A changelog that times out must not stop someone finding out
        whether they can upgrade."""
        with patch(_CHECK, return_value=NEWER), patch(_NOTES, side_effect=OSError("timeout")):
            ucli._print_release_notes()  # must not raise

    def test_a_failed_version_check_is_silent_too(self, capsys):
        with patch(_CHECK, side_effect=OSError("offline")):
            ucli._print_release_notes()
        assert capsys.readouterr().out == ""

    def test_no_notes_still_announces_the_version(self, capsys):
        """An empty changelog is a reason to say less, not to say nothing:
        the upgrade is still available and the user still has to decide."""
        with patch(_CHECK, return_value=NEWER), patch(_NOTES, return_value=""):
            ucli._print_release_notes()
        assert "1.14.0" in capsys.readouterr().out
