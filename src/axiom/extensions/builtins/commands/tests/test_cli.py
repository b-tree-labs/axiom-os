# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `axi commands` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from axiom.extensions.builtins.commands import cli, discovery, state


def _stub_tree() -> discovery.CommandTree:
    tree = discovery.CommandTree()
    tree.nouns["tidy"] = discovery.CliNoun(
        noun="tidy",
        extension="hygiene",
        description="hygiene steward",
        module="axiom.extensions.builtins.hygiene.cli",
        function="main",
        tier="builtin",
        verbs=(discovery.Verb(name="status", help="Show status"),),
    )
    tree.slash_commands["help"] = discovery.SlashCommand(
        name="help", extension="chat", description="show help"
    )
    return tree


def _stub_state_dir(tmp_path: Path):
    return patch(
        "axiom.extensions.builtins.commands.cli.get_user_state_dir",
        return_value=tmp_path,
    )


def test_generate_writes_files_for_one_harness(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        rc = cli.main(["generate", "--harness", "claude", "--out-dir", str(out)])
    assert rc == 0
    assert (out / ".claude/commands/axi/tidy/status.md").exists()
    # State updated
    entries = state.load(tmp_path)
    assert any(e.harness == "claude" for e in entries)


def test_generate_all_writes_each_harness(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        rc = cli.main(["generate", "--harness", "all", "--out-dir", str(out)])
    assert rc == 0
    entries = {e.harness for e in state.load(tmp_path)}
    assert entries == set(cli.HARNESSES.keys())


def test_generate_strict_aborts_on_conflict(tmp_path):
    tree = _stub_tree()
    tree.conflicts.append(
        discovery.Conflict(
            key="tidy", winner_extension="hygiene", loser_extension="other", reason="alphabetical-tiebreak"
        )
    )
    out = tmp_path / "out"
    out.mkdir()
    with (
        patch.object(cli, "discover_command_tree", return_value=tree),
        _stub_state_dir(tmp_path),
    ):
        rc = cli.main(
            ["generate", "--harness", "claude", "--out-dir", str(out), "--strict"]
        )
    assert rc == 2


def test_generate_dry_run_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        rc = cli.main(
            ["generate", "--harness", "claude", "--out-dir", str(out), "--dry-run"]
        )
    assert rc == 0
    assert not list(out.rglob("*.md"))
    assert "[dry-run]" in capsys.readouterr().out


def test_list_shows_nouns_and_slashes(tmp_path, capsys):
    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        rc = cli.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tidy" in out
    assert "/help" in out


def test_list_with_conflicts_flag(tmp_path, capsys):
    tree = _stub_tree()
    tree.conflicts.append(
        discovery.Conflict(
            key="tidy", winner_extension="hygiene", loser_extension="rival", reason="lower-tier"
        )
    )
    with (
        patch.object(cli, "discover_command_tree", return_value=tree),
        _stub_state_dir(tmp_path),
    ):
        cli.main(["list", "--conflicts"])
    out = capsys.readouterr().out
    assert "shadowed=rival" in out


def test_regenerate_refreshes_all_state_entries(tmp_path):
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    out_a.mkdir()
    out_b.mkdir()
    state.upsert(tmp_path, "claude", out_a, 0)
    state.upsert(tmp_path, "vscode", out_b, 0)

    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        rc = cli.main(["regenerate"])
    assert rc == 0
    assert (out_a / ".claude/commands/axi/tidy/status.md").exists()
    assert (out_b / ".vscode/tasks.json").exists()


def test_regenerate_with_no_state_is_noop(tmp_path, capsys):
    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        rc = cli.main(["regenerate"])
    assert rc == 0
    assert "No previously-generated shims" in capsys.readouterr().out


def test_state_round_trip(tmp_path):
    state.upsert(tmp_path, "claude", tmp_path / "a", 5)
    state.upsert(tmp_path, "claude", tmp_path / "a", 7)  # upsert replaces
    entries = state.load(tmp_path)
    assert len(entries) == 1
    assert entries[0].file_count == 7


def test_unknown_harness_exits_2(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        try:
            cli.main(["generate", "--harness", "nope", "--out-dir", str(out)])
            raised = False
        except SystemExit as e:
            raised = True
            assert e.code == 2
        assert raised


def test_manifest_validates():
    """Smoke: AEOS schema accepts the new commands extension."""
    import tomllib

    from axiom_tests.unit_tests.extension import build_validator, validate_manifest

    manifest = (
        Path(__file__).parents[1] / "axiom-extension.toml"
    )
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    errors = validate_manifest(data, validator=build_validator())
    assert not errors, "\n  ".join(errors)


def test_axi_update_hook_invokes_regenerate_all(tmp_path):
    """Smoke: regenerate_all is exposed for axi update to call."""
    state.upsert(tmp_path, "claude", tmp_path, 0)
    called: dict[str, bool] = {"yes": False}

    original = cli._cmd_regenerate

    def spy(args):
        called["yes"] = True
        return original(args)

    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
        patch.object(cli, "_cmd_regenerate", side_effect=spy),
    ):
        cli.regenerate_all()
    assert called["yes"]


def test_generate_writes_idempotent(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with (
        patch.object(cli, "discover_command_tree", return_value=_stub_tree()),
        _stub_state_dir(tmp_path),
    ):
        cli.main(["generate", "--harness", "vscode", "--out-dir", str(out)])
        first = json.loads((out / ".vscode/mcp.json").read_text())
        cli.main(["generate", "--harness", "vscode", "--out-dir", str(out)])
        second = json.loads((out / ".vscode/mcp.json").read_text())
    assert first == second
