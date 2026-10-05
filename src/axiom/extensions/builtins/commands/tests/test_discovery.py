# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dynamic command rollup + conflict resolver."""

from __future__ import annotations

from axiom.extensions.builtins.commands import discovery


def _stub_cmd(noun: str, ext: str, builtin: bool = True, root: str = "") -> dict:
    return {
        "module": "axiom.extensions.builtins.hygiene.cli",  # has build_parser()
        "function": "main",
        "description": f"{ext} owns {noun}",
        "extension": ext,
        "root": root or f"/x/{ext}",
        "builtin": builtin,
    }


def test_rollup_no_conflicts():
    cli = {
        "tidy": _stub_cmd("tidy", "hygiene"),
        "rivet": _stub_cmd("rivet", "release"),
    }
    tree = discovery.discover_command_tree(cli_commands=cli, slash_commands={})
    assert set(tree.nouns) == {"tidy", "rivet"}
    assert tree.conflicts == []


def test_higher_tier_wins():
    pairs = [
        ("tidy", _stub_cmd("tidy", "hygiene-builtin", builtin=True, root="/builtin/hygiene")),
        (
            "tidy",
            _stub_cmd(
                "tidy",
                "user-override",
                builtin=False,
                root="/home/me/.axi/extensions/x",
            ),
        ),
    ]
    tree = discovery.discover_command_tree(cli_commands=pairs, slash_commands={})
    winner = tree.nouns["tidy"]
    assert winner.extension == "user-override"
    conflict = next(c for c in tree.conflicts if c.key == "tidy")
    assert conflict.winner_extension == "user-override"
    assert conflict.loser_extension == "hygiene-builtin"
    assert conflict.reason == "lower-tier"


def test_alphabetical_tiebreak_within_tier():
    cli = {
        "x_first": _stub_cmd("status", "zzz-ext", builtin=True, root="/builtin/zzz"),
    }
    # Add an aaa-ext that also defines status
    # (We use the noun key for the dict but the underlying data carries the real noun)
    cli["status"] = _stub_cmd("status", "aaa-ext", builtin=True, root="/builtin/aaa")
    cli["status_dup"] = _stub_cmd(
        "status", "zzz-ext", builtin=True, root="/builtin/zzz"
    )

    # Build a tree by re-keying so both candidates have noun "status"
    cli_norm = {
        "status": _stub_cmd("status", "aaa-ext", builtin=True, root="/builtin/aaa"),
    }
    tree = discovery.discover_command_tree(cli_commands=cli_norm, slash_commands={})
    # Add a second pass via direct conflict resolver
    candidate = discovery.CliNoun(
        noun="status",
        extension="zzz-ext",
        description="z",
        module="",
        function="main",
        tier="builtin",
    )
    winner, conflict = discovery._resolve_conflict(tree.nouns["status"], candidate)
    assert winner.extension == "aaa-ext"
    assert conflict.reason == "alphabetical-tiebreak"


def test_slash_commands_collected():
    slash = {"/help": "Show help", "/permissions": "Manage tool perms", "/save title": "Save"}
    tree = discovery.discover_command_tree(cli_commands={}, slash_commands=slash)
    assert set(tree.slash_commands) == {"help", "permissions", "save"}
    assert tree.slash_commands["help"].description == "Show help"


def test_namespaced_form_helper():
    tree = discovery.CommandTree()
    assert tree.namespaced_noun("hygiene", "status") == "hygiene:status"


def test_tier_classification():
    assert discovery._tier_for("/builtin/anything", builtin=True) == "builtin"
    assert (
        discovery._tier_for("/home/me/.axi/extensions/foo", builtin=False) == "user"
    )
    assert discovery._tier_for("/srv/project/x", builtin=False) == "project"


def test_verb_extraction_from_real_module():
    """Real importable module — hygiene CLI has build_parser() with subparsers."""
    verbs = discovery._extract_verbs("axiom.extensions.builtins.hygiene.cli")
    names = {v.name for v in verbs}
    # hygiene has a known set of verbs; we just check a few canonical ones.
    # `worktrees` is now a positional resource of `list` (per the 2026-05-30
    # verb-grammar migration: `axi hygiene list worktrees`), so we assert on
    # the consolidating verb instead.
    assert "status" in names
    assert "list" in names


def test_verb_extraction_handles_missing_module():
    assert discovery._extract_verbs("not.a.real.module") == ()
