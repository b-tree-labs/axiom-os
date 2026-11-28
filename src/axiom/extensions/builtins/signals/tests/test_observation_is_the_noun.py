# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-265 step 1 (D9): the CLI noun is ``observation``; ``signal`` is its deprecated spelling.

Every old command still works for one release. The old noun runs the same command and
prints one line on stderr naming the new spelling and the release that removes the old
one, as ``secrets`` did for ``vault`` (ADR-218). ``AXI_NO_DEPRECATION_WARNING=1`` hides it.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from axiom.extensions.builtins.signals import cli, deprecated_cli

MANIFEST = Path(cli.__file__).with_name("axiom-extension.toml")


def _cmds():
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    return {p["noun"]: p for p in data["extension"]["provides"] if p.get("kind") == "cmd"}


def test_observation_is_the_noun_and_signal_is_the_deprecated_alias():
    cmds = _cmds()
    assert cmds["observation"]["entry"] == "axiom.extensions.builtins.signals.cli:main"
    assert cmds["signal"]["entry"] == "axiom.extensions.builtins.signals.deprecated_cli:main"
    assert "Deprecated" in cmds["signal"]["description"]


def test_the_old_noun_runs_the_same_command_and_says_what_replaced_it(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(cli, "main", lambda: ran.append(list(sys.argv[1:])))
    monkeypatch.setattr(sys, "argv", ["axi-signal", "brief", "--since", "1d"])
    monkeypatch.delenv(deprecated_cli.QUIET_ENV, raising=False)
    deprecated_cli.main()
    assert ran == [["brief", "--since", "1d"]]
    err = capsys.readouterr().err
    assert "observation brief" in err and "deprecated" in err and "v0.72.0" in err


def test_the_line_can_be_hidden(monkeypatch, capsys):
    monkeypatch.setattr(cli, "main", lambda: None)
    monkeypatch.setattr(sys, "argv", ["axi-signal", "status"])
    monkeypatch.setenv(deprecated_cli.QUIET_ENV, "1")
    deprecated_cli.main()
    assert capsys.readouterr().err == ""
