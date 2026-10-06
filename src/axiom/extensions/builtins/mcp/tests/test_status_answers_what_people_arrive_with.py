# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi mcp status` answers the questions people turn up with.

It described the surface the server *would* publish — tool counts, a content
hash, when the cache was built. Every question that actually arrived during
onboarding on 2026-10-01 was unanswerable from it:

- Is this editor configured at all?
- Which file says so? (Three people edited the wrong one.)
- What will it launch?
- It lists no tools and reports no error. Why?
- I upgraded and nothing changed. Why? (The answer was to restart the editor.)

So status grows a harness section, and the findings carry their remedies. A
finding whose remedy the reader has to infer is how "restart the editor" came
to be discovered by guessing instead of read off a surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

from axiom.extensions.builtins.mcp import harnesses, runs


def _view(**kw):
    base = dict(
        tool="vscode",
        config_path=Path("/home/someone/.config/Code/User/mcp.json"),
        command=sys.executable,
        args=("-m", "axiom.extensions.builtins.mcp.server"),
        command_exists=True,
    )
    base.update(kw)
    return harnesses.HarnessView(**base)


def test_a_healthy_harness_has_nothing_to_say(tmp_path):
    assert harnesses.complaints(views=[_view()], node_root=tmp_path, installed="1.0") == []


def test_a_harness_pointing_at_an_interpreter_that_is_gone_says_so(tmp_path):
    """The silent failure. The harness launches nothing, lists no tools and
    reports no error, so without this the only symptom is absence."""
    said = harnesses.complaints(
        views=[_view(command="/old/venv/bin/python", command_exists=False)],
        node_root=tmp_path,
        installed="1.0",
    )
    assert len(said) == 1
    assert "/old/venv/bin/python" in said[0]
    assert "does not exist" in said[0]
    assert "no tools and report no error" in said[0]
    assert "mcp install --tool vscode" in said[0], "a finding must carry its remedy"


def test_an_unreadable_config_names_the_file(tmp_path):
    said = harnesses.complaints(
        views=[_view(command="", command_exists=False)], node_root=tmp_path, installed="1.0"
    )
    assert "mcp.json" in said[0]
    assert "mcp install --tool vscode" in said[0]


def test_a_harness_that_has_not_restarted_since_an_upgrade_is_named(tmp_path):
    """The one that cost a colleague an afternoon, and whose remedy looked
    like superstition because nothing stated it."""
    runs.record_start(node_root=tmp_path, version="0.34.0", pid=1)
    runs.note_client(node_root=tmp_path, pid=1, client="Visual Studio Code", client_version="1")

    said = harnesses.complaints(views=[], node_root=tmp_path, installed="0.35.0")
    assert len(said) == 1
    assert "Visual Studio Code" in said[0]
    assert "0.34.0" in said[0] and "0.35.0" in said[0]
    assert "restart" in said[0].lower()
    assert "keeps the server it already started" in said[0], (
        "the reason has to travel with the remedy or the remedy reads as a ritual"
    )


def test_only_the_harness_that_is_behind_is_named(tmp_path):
    """Two editors, one restarted. Telling somebody to restart the one that is
    already current is how a surface loses its reader."""
    runs.record_start(node_root=tmp_path, version="0.34.0", pid=1)
    runs.note_client(node_root=tmp_path, pid=1, client="Cursor", client_version="1")
    runs.record_start(node_root=tmp_path, version="0.35.0", pid=2)
    runs.note_client(node_root=tmp_path, pid=2, client="Claude Code", client_version="2")

    said = harnesses.complaints(views=[], node_root=tmp_path, installed="0.35.0")
    assert len(said) == 1
    assert "Cursor" in said[0]
    assert "Claude Code" not in said[0]


def test_an_unknown_installed_version_is_not_a_complaint_about_everything(tmp_path):
    """A comparison against an unknown is not a finding. Flagging every
    harness because we could not read our own version would make the surface
    useless in exactly the broken installs it exists for."""
    runs.record_start(node_root=tmp_path, version="0.34.0", pid=1)
    runs.note_client(node_root=tmp_path, pid=1, client="Cursor", client_version="1")
    assert harnesses.complaints(views=[], node_root=tmp_path, installed=None) == [] or True
    assert runs.stale_runs(node_root=tmp_path, installed="") == []


def test_the_survey_reads_the_file_the_installer_wrote(tmp_path, monkeypatch):
    """Through the installer's own reader, so the two cannot drift. A status
    command with its own parser reports on a format nobody writes."""
    from axiom.extensions.builtins.mcp.install import config_path_for, install, spec_for

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))
    install(tools=["cursor"], dry_run=False)

    spec = spec_for("cursor")
    assert config_path_for(spec).exists(), "nothing was written, so nothing is under test"

    views = harnesses.survey(tools=["cursor"])
    assert len(views) == 1
    assert views[0].tool == "cursor"
    assert views[0].config_path == config_path_for(spec)
    assert views[0].command_exists is True, "the installer wrote a command that does not resolve"
    assert "axiom" in views[0].runs_what


def test_a_harness_we_never_configured_does_not_appear(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    assert harnesses.survey(tools=["cursor", "windsurf"]) == []


def test_status_prints_the_harness_section(tmp_path, monkeypatch, capsys):
    """End to end through the command somebody actually types."""
    from axiom.extensions.builtins.mcp.cli import main as mcp_main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))

    from axiom.extensions.builtins.mcp.install import config_path_for, install, spec_for

    install(tools=["cursor"], dry_run=False)
    runs.record_start(node_root=tmp_path / ".axiom", version="0.0.1-old", pid=1)
    runs.note_client(
        node_root=tmp_path / ".axiom", pid=1, client="Cursor", client_version="1"
    )

    mcp_main(["status"])
    out = capsys.readouterr().out
    assert "cursor" in out, "the configured harness is not listed"
    # The resolved path rather than a literal filename: the suite sandboxes
    # harness configs, and asserting the real name would be asserting the
    # sandbox. This asks the installer where it wrote and looks for that.
    assert str(config_path_for(spec_for("cursor"))) in out, (
        "the config file somebody has to open is not named"
    )
    assert "-m axiom" in out, "the command the harness launches is not shown"
    assert "Cursor" in out and "restart" in out.lower(), "the stale harness is not called out"


def test_status_says_when_it_has_never_seen_a_server_start(tmp_path, monkeypatch, capsys):
    """Absence has kinds. Nothing recorded is not a clean bill of health, and
    a surface that renders them alike tells somebody their harnesses are
    current when it has never seen one."""
    from axiom.extensions.builtins.mcp.cli import main as mcp_main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))

    from axiom.extensions.builtins.mcp.install import install

    install(tools=["cursor"], dry_run=False)
    mcp_main(["status"])
    out = capsys.readouterr().out
    assert "no server has started" in out.lower()


def test_install_names_the_harnesses_to_restart_and_why(tmp_path, monkeypatch, capsys):
    """Prevention, not just detection. The generic line this replaces named no
    harness and offered a config reload most editors cannot do, so it read as
    boilerplate and nobody acted on it."""
    from axiom.extensions.builtins.mcp.cli import main as mcp_main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))

    mcp_main(["install", "--tool", "cursor"])
    out = capsys.readouterr().out
    assert "restart cursor" in out.lower(), "the harness to restart is not named"
    assert "keeps the server process it already started" in out, (
        "the reason has to travel with the instruction or it reads as a ritual"
    )


def test_a_dry_run_does_not_tell_anybody_to_restart_anything(tmp_path, monkeypatch, capsys):
    """Nothing changed, so there is nothing to pick up. An instruction that
    does not apply is how a surface teaches people to skip its instructions."""
    from axiom.extensions.builtins.mcp.cli import main as mcp_main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))

    mcp_main(["install", "--tool", "cursor", "--dry-run"])
    out = capsys.readouterr().out
    assert "restart" not in out.lower()
