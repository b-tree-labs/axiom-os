# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext completion <shell>``."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.completion import CompletionProvider, snippet_for
from axiom.cli.ext.provider import CliContext


def _run(tmp_path: Path, *argv: str) -> int:
    provider = CompletionProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    return provider.run(args, CliContext(cwd=tmp_path))


class TestSnippets:
    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_snippet_mentions_register(self, shell: str):
        snippet = snippet_for(shell)
        assert "register-python-argcomplete" in snippet

    def test_unknown_shell_keyerror(self):
        with pytest.raises(KeyError):
            snippet_for("powershell")


class TestCli:
    def test_zsh_output(self, tmp_path: Path, capsys):
        assert _run(tmp_path, "zsh") == 0
        out = capsys.readouterr().out
        assert "register-python-argcomplete" in out
        assert "axi" in out

    def test_bash_output(self, tmp_path: Path, capsys):
        assert _run(tmp_path, "bash") == 0
        out = capsys.readouterr().out
        assert "register-python-argcomplete axi" in out

    def test_fish_output(self, tmp_path: Path, capsys):
        assert _run(tmp_path, "fish") == 0
        out = capsys.readouterr().out
        assert "register-python-argcomplete" in out
        assert "fish" in out

    def test_rejects_unknown_shell(self, tmp_path: Path):
        # argparse choices enforcement — SystemExit (non-zero) on unknown.
        with pytest.raises(SystemExit) as exc:
            _run(tmp_path, "powershell")
        assert exc.value.code != 0


def test_completion_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "completion" in providers
