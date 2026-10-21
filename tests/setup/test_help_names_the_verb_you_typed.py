"""`start` prints help that says `start`.

`start` shipped as the verb a newcomer types (0.63.0), dispatching to the same
wizard as `config`. Its help still titled itself `config`, so the first screen
a newcomer read told them the command was something other than what they had
just typed. Dispatch sets ``sys.argv[0]`` to ``"<cli> <verb>"``, which is the
one place the typed verb is known.
"""

from __future__ import annotations

import sys

from axiom.setup import cli


def _help(capsys, monkeypatch, argv0: str) -> str:
    monkeypatch.setattr(sys, "argv", [argv0, "--help"])
    cli.main()
    return capsys.readouterr().out


def test_start_help_says_start(capsys, monkeypatch):
    out = _help(capsys, monkeypatch, "neut start")
    assert out.splitlines()[0].startswith("neut start")
    assert "neut config" not in out


def test_config_help_still_says_config(capsys, monkeypatch):
    out = _help(capsys, monkeypatch, "axi config")
    assert out.splitlines()[0].startswith("axi config")


def test_a_bare_program_name_falls_back_to_config(capsys, monkeypatch):
    out = _help(capsys, monkeypatch, "/usr/bin/python")
    assert " config" in out.splitlines()[0]
