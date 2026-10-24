# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext init --interactive`` — guided scaffold wizard."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest

from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.provider import CliContext


def _run(argv: list[str], cwd: Path) -> int:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(argv)
    ctx = CliContext(cwd=cwd)
    return provider.run(args, ctx)


# ---------------------------------------------------------------------------
# Non-TTY refusal
# ---------------------------------------------------------------------------


def test_wizard_refuses_without_tty(tmp_path: Path, capsys) -> None:
    """In a non-TTY context, --interactive must error with a clear message."""
    # pytest's capsys replaces sys.stdin/out with non-TTY streams, so this
    # matches the CI / pipe scenario by default.
    rc = _run(["--interactive", "--dir", str(tmp_path)], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "TTY" in err or "tty" in err


def test_missing_name_in_non_tty_errors_clearly(
    tmp_path: Path, capsys
) -> None:
    """No name + no --interactive in a non-TTY must surface a clear error."""
    rc = _run(["--dir", str(tmp_path)], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    # Either the name-required message or a wizard hint is acceptable.
    assert "name" in err.lower() or "interactive" in err.lower()


# ---------------------------------------------------------------------------
# TTY path — canned prompt answers
# ---------------------------------------------------------------------------


@pytest.fixture
def force_tty(monkeypatch: pytest.MonkeyPatch):
    """Fake sys.stdin + sys.stdout as TTYs so the wizard path is taken."""
    import sys

    class _FakeTTY:
        def __init__(self, wrapped):
            self._w = wrapped

        def isatty(self) -> bool:
            return True

        def __getattr__(self, item):
            return getattr(self._w, item)

    monkeypatch.setattr(sys, "stdin", _FakeTTY(sys.stdin))
    # stdout stays as the capsys stream; we only need stdin.isatty() to be True
    # for the wizard entry check — the Prompt.ask calls are mocked.


def test_wizard_happy_path(
    tmp_path: Path, force_tty, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Rich prompts are monkeypatched; wizard scaffolds the normal layout."""
    answers = iter(
        [
            "greeter",  # name
            "b-tree-labs",  # owner
            "apache",  # license (fuzzy)
            "greeter — hello world",  # description
            "compound",  # template (only one available)
        ]
    )

    from axiom.cli.ext.commands import init as init_mod

    monkeypatch.setattr(init_mod.Prompt, "ask", lambda *a, **kw: next(answers))
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *a, **kw: True)

    rc = _run(["--interactive", "--dir", str(tmp_path)], tmp_path)
    assert rc == 0
    ext = tmp_path / "greeter"
    assert ext.is_dir()
    manifest = tomllib.loads((ext / "axiom-extension.toml").read_text())
    assert manifest["extension"]["name"] == "greeter"
    assert manifest["extension"]["license"] == "Apache-2.0"
    assert manifest["extension"]["owner"] == "b-tree-labs"


def test_wizard_reprompts_on_bad_name(
    tmp_path: Path, force_tty, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A name that fails validate_name triggers a reprompt, not an abort."""
    answers = iter(
        [
            "my_agent",  # reject — type suffix
            "my_ext",  # accept
            "b-tree-labs",
            "Apache-2.0",
            "",  # default description
            "compound",
        ]
    )

    from axiom.cli.ext.commands import init as init_mod

    monkeypatch.setattr(init_mod.Prompt, "ask", lambda *a, **kw: next(answers))
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *a, **kw: True)

    rc = _run(["--interactive", "--dir", str(tmp_path)], tmp_path)
    assert rc == 0
    assert (tmp_path / "my_ext").is_dir()
    assert not (tmp_path / "my_agent").exists()


def test_wizard_fuzzy_license_accepted(
    tmp_path: Path, force_tty, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A fuzzy license input is canonicalized in the resulting manifest."""
    answers = iter(
        [
            "foo",
            "b-tree-labs",
            "apache",  # fuzzy
            "",
            "compound",
        ]
    )
    from axiom.cli.ext.commands import init as init_mod

    monkeypatch.setattr(init_mod.Prompt, "ask", lambda *a, **kw: next(answers))
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *a, **kw: True)

    rc = _run(["--interactive", "--dir", str(tmp_path)], tmp_path)
    assert rc == 0
    manifest = tomllib.loads(
        (tmp_path / "foo" / "axiom-extension.toml").read_text()
    )
    assert manifest["extension"]["license"] == "Apache-2.0"


def test_wizard_abort_leaves_no_files(
    tmp_path: Path, force_tty, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """User declines final confirmation; nothing written, exit 0."""
    answers = iter(
        [
            "foo",
            "b-tree-labs",
            "Apache-2.0",
            "",
            "compound",
        ]
    )
    from axiom.cli.ext.commands import init as init_mod

    monkeypatch.setattr(init_mod.Prompt, "ask", lambda *a, **kw: next(answers))
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *a, **kw: False)

    rc = _run(["--interactive", "--dir", str(tmp_path)], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "abort" in out.lower()
    assert not (tmp_path / "foo").exists()


def test_wizard_no_positional_name_triggers_interactive(
    tmp_path: Path, force_tty, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Bare `axi ext init` in a TTY should walk the wizard."""
    answers = iter(
        [
            "bare_name",
            "b-tree-labs",
            "Apache-2.0",
            "",
            "compound",
        ]
    )
    from axiom.cli.ext.commands import init as init_mod

    monkeypatch.setattr(init_mod.Prompt, "ask", lambda *a, **kw: next(answers))
    monkeypatch.setattr(init_mod.Confirm, "ask", lambda *a, **kw: True)

    rc = _run(["--dir", str(tmp_path)], tmp_path)
    assert rc == 0
    assert (tmp_path / "bare_name").is_dir()
