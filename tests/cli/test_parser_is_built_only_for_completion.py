# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The argparse tree is built for tab completion, and for nothing else.

`get_parser()` imports EVERY builtin extension's module, because a child
parser is the only way to learn an extension's sub-verbs. That pulls FastAPI,
SQLAlchemy and the MCP type system into a process that may be about to print
one line of help. Measured on a released install: `get_parser()` is 1458ms of
a ~1500ms `axi --help`, against 191ms to read all 68 manifests and 24ms to
import the CLI module itself.

It was paid on every invocation and then discarded, because
`argcomplete.autocomplete()` opens with::

    if "_ARGCOMPLETE" not in os.environ:
        return

So the shell asks for completions a few times a day, and every other command
anyone or any agent ran bought a parse tree for nothing. Dispatch never used
it — it resolves the subcommand from the manifest and imports that one module.

This test pins the contract in both directions, because the cheap mistake
here is silently disabling completion to make a benchmark look good.
"""

from __future__ import annotations

import sys

import pytest

import axiom.axiom_cli as cli


@pytest.fixture
def spy(monkeypatch):
    """Count `get_parser()` calls without paying for one."""
    calls: list[int] = []

    def counted():
        calls.append(1)
        import argparse

        return argparse.ArgumentParser(prog="axi")

    monkeypatch.setattr(cli, "get_parser", counted)
    return calls


@pytest.fixture(autouse=True)
def _no_completion_env(monkeypatch):
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)


def _run(argv):
    sys.argv = argv
    try:
        cli.main()
    except SystemExit:
        pass


class TestTheOrdinaryCase:
    """Every command a human or an agent actually runs."""

    def test_help_does_not_build_the_tree(self, spy, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["axi", "--help"])
        _run(["axi", "--help"])
        assert spy == [], "building the parser for --help costs ~1.4s and is discarded"

    def test_version_does_not_build_the_tree(self, spy):
        _run(["axi", "--version"])
        assert spy == []

    def test_a_bare_invocation_does_not_build_the_tree(self, spy):
        _run(["axi"])
        assert spy == []


class TestCompletionStillWorks:
    """The saving is only legitimate if the feature it skipped survives."""

    def test_the_tree_is_built_when_the_shell_asks(self, monkeypatch):
        """argcomplete's own gate is the word index the shell exports."""
        calls: list[int] = []

        def counted():
            calls.append(1)
            import argparse

            return argparse.ArgumentParser(prog="axi")

        seen: list[object] = []
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        # The eager path: with the completion index off, the tree is built from
        # get_parser() on every press, as it always was. The cached path is
        # covered by test_the_tree_is_built_once_then_read_from_the_index.
        monkeypatch.setenv("AXIOM_INDEX_CACHE", "0")
        monkeypatch.setattr(cli, "get_parser", counted)
        # Stub the completer itself: the real one talks to the shell over a
        # dedicated fd and would need the whole COMP_* protocol set up. What
        # this test is about is whether it gets a parser at all.
        import argcomplete

        monkeypatch.setattr(argcomplete, "autocomplete", seen.append)

        _run(["axi"])

        assert calls == [1], "with _ARGCOMPLETE set the tree must still be built"
        assert len(seen) == 1, "and it must be handed to the completer"

    def test_the_tree_is_built_once_then_read_from_the_index(self, monkeypatch, tmp_path):
        """With the index on, the first press builds the tree (ungated, so it
        can be stored) and later presses build nothing: the completer is handed
        a parser rebuilt from the stored tree."""
        import argparse

        calls: list[dict] = []

        def counted(**kwargs):
            calls.append(kwargs)
            top = argparse.ArgumentParser(prog="axi")
            sub = top.add_subparsers(dest="subcommand")
            sub.add_parser("widget", help="a widget")
            return top

        seen: list[argparse.ArgumentParser] = []
        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.delenv("AXIOM_INDEX_CACHE", raising=False)
        monkeypatch.delenv("AXI_FULL_PARSER", raising=False)
        monkeypatch.setattr(cli, "get_parser", counted)
        import argcomplete

        monkeypatch.setattr(argcomplete, "autocomplete", seen.append)

        _run(["axi"])
        assert calls == [{"gate": False}], "the first press builds the tree, ungated"
        _run(["axi"])
        assert calls == [{"gate": False}], "the second press must read the stored tree"
        assert len(seen) == 2
        for parser in seen:
            names = {n for a in parser._actions if isinstance(a, argparse._SubParsersAction) for n in a.choices}
            assert "widget" in names

    def test_a_missing_argcomplete_is_not_a_crash(self, monkeypatch):
        """The import is optional; its absence must not take the CLI down."""
        import builtins

        real_import = builtins.__import__

        def refuse(name, *a, **k):
            if name == "argcomplete":
                raise ImportError("no argcomplete")
            return real_import(name, *a, **k)

        monkeypatch.setenv("_ARGCOMPLETE", "1")
        monkeypatch.setattr(cli, "get_parser", lambda: None)
        monkeypatch.setattr(builtins, "__import__", refuse)

        _run(["axi", "--version"])  # must not raise
