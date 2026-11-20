# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi data enroll` prints the next command. It has to be one that runs.

Enrolling a source is deliberately not the same act as minting its credential, so
`enroll` prints the `axi gate issue` line rather than running it. But that command
needs the gate's API-keys file — from `--keys-file` or its environment variable,
with NO default — and stops with "no keys file" without one.

A deployed node's service sets that variable. Somebody enrolling a partner's
source by hand, in their own shell, often has not, and they are exactly who reads
this line. Found by doing it: the printed command failed on the first try.

The note is conditional, following the reader-extra advice in the DAQ tree: a note
that is wrong the first time somebody can check it is a note they stop believing,
and the rest of this output — the connector name, the audit receipt, the model
attribution — is load-bearing.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.skills.enroll import (
    issue_command,
    issue_prerequisite_note,
)

SOURCE = {"name": "pxi", "site": "utne-triga"}


class TestTheCommandItself:
    def test_it_names_the_verb_and_the_principal(self):
        command = issue_command(SOURCE, "utne-triga-pxi")
        assert command.startswith("axi gate issue api-key")
        assert "--principal @svc-utne-triga:utne-triga" in command

    def test_it_binds_the_key_to_the_site(self):
        """An unbound key is one that can write for any tenant."""
        assert "--site utne-triga" in issue_command(SOURCE, "utne-triga-pxi")

    def test_it_names_what_the_key_is_for(self):
        """`axi gate list api-keys` shows this, and in six months it is the only
        thing that says which key belongs to which source."""
        assert "utne-triga-pxi ingest" in issue_command(SOURCE, "utne-triga-pxi")


class TestThePrerequisiteNote:
    def test_it_appears_when_the_keys_file_is_not_configured(self, monkeypatch):
        from axiom.extensions.builtins.webgate.skills._accounts import KEYS_ENV

        monkeypatch.delenv(KEYS_ENV, raising=False)
        note = issue_prerequisite_note()
        assert note
        assert "--keys-file" in note
        assert KEYS_ENV in note

    def test_it_says_there_is_no_default(self, monkeypatch):
        """Which is the part that makes it a prerequisite rather than a detail."""
        from axiom.extensions.builtins.webgate.skills._accounts import KEYS_ENV

        monkeypatch.delenv(KEYS_ENV, raising=False)
        assert "no default" in issue_prerequisite_note()

    def test_it_says_the_files_shape(self, monkeypatch):
        """An empty `{}` is refused with "top level must be a list of key
        objects" — a clear message, and one worth not needing."""
        from axiom.extensions.builtins.webgate.skills._accounts import KEYS_ENV

        monkeypatch.delenv(KEYS_ENV, raising=False)
        note = issue_prerequisite_note()
        assert "JSON list" in note and "[]" in note

    def test_it_is_silent_when_the_variable_is_set(self, monkeypatch):
        """The conditional half. On a deployed node the service sets it, and a
        note repeated there is noise that teaches the reader to skim."""
        from axiom.extensions.builtins.webgate.skills._accounts import KEYS_ENV

        monkeypatch.setenv(KEYS_ENV, "/etc/axiom/api-keys.json")
        assert issue_prerequisite_note() == ""

    def test_it_reuses_the_gates_own_name_for_the_variable(self):
        """Not a second spelling of it. A literal here would drift the day the
        gate renames it, and this note would then advise a variable nothing
        reads."""
        import inspect

        from axiom.extensions.builtins.data_platform.skills import enroll

        source = inspect.getsource(enroll.issue_prerequisite_note)
        assert "KEYS_ENV" in source
        assert "AXIOM_GATE_API_KEYS_FILE" not in source

    def test_it_says_nothing_when_there_is_no_gate_to_advise_about(
        self, monkeypatch
    ):
        """A composition without the gate installed cannot be told about its
        keys file, and guessing would be worse than silence."""
        import builtins

        real_import = builtins.__import__

        def _no_webgate(name, *args, **kwargs):
            if "webgate" in name:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_webgate)
        assert issue_prerequisite_note() == ""
