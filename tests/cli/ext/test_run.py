# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext run`` — execute a declared capability."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from axiom.cli.ext.commands.run import RunProvider, _parse_capability_spec, _resolve_entry
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def run_run_cli(capsys):
    def _run(*argv: str) -> tuple[int, str]:
        capsys.readouterr()
        provider = RunProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args(list(argv))
        ctx = CliContext(cwd=Path.cwd())
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_run_provider_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "run" in providers


# ---------------------------------------------------------------------------
# Capability spec parser
# ---------------------------------------------------------------------------


def test_parse_capability_spec_valid() -> None:
    assert _parse_capability_spec("foo.cmd.bar") == ("foo", "cmd", "bar")


def test_parse_capability_spec_multi_dot_name() -> None:
    # Name segment may include additional dots; kind is fixed vocabulary.
    assert _parse_capability_spec("foo.cmd.some.nested.name") == (
        "foo",
        "cmd",
        "some.nested.name",
    )


@pytest.mark.parametrize(
    "bad",
    ["only.two", "foo..bar", ""],
)
def test_parse_capability_spec_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_capability_spec(bad)


def test_parse_capability_spec_bare_ext_is_valid() -> None:
    """Unit 9: bare ``<ext>`` form is accepted — dispatcher picks the cmd."""
    assert _parse_capability_spec("foo") == ("foo", None, None)


# ---------------------------------------------------------------------------
# End-to-end: a fake extension with a cmd capability. We bypass the installed
# package discovery and drive _resolve_entry directly with a manifest on disk.
# ---------------------------------------------------------------------------


def _write_fake_ext_with_cmd(ext_path: Path, entry: str) -> None:
    ext_path.mkdir(parents=True, exist_ok=True)
    (ext_path / "axiom-extension.toml").write_text(
        "[extension]\n"
        'name = "fake"\n'
        'version = "0.1.0"\n'
        'description = "fake"\n'
        'license = "Apache-2.0"\n'
        'aeos_version = "0.1.0"\n\n'
        "[[extension.provides]]\n"
        'kind = "cmd"\n'
        'noun = "hello"\n'
        f'entry = "{entry}"\n'
    )


def test_resolve_entry_finds_cmd_block(tmp_path: Path) -> None:
    _write_fake_ext_with_cmd(tmp_path / "fake", "fake.commands.hello:main")
    entry = _resolve_entry(tmp_path / "fake", kind="cmd", name="hello")
    assert entry == "fake.commands.hello:main"


def test_resolve_entry_returns_none_when_missing(tmp_path: Path) -> None:
    _write_fake_ext_with_cmd(tmp_path / "fake", "fake.commands.hello:main")
    assert _resolve_entry(tmp_path / "fake", kind="cmd", name="missing") is None


# ---------------------------------------------------------------------------
# CLI behavior
# ---------------------------------------------------------------------------


def test_run_unknown_extension_returns_one(run_run_cli) -> None:
    rc, out = run_run_cli("definitely_not_installed.cmd.whatever")
    assert rc == 1
    assert "axi ext list" in out.lower() or "not installed" in out.lower()


def test_run_non_cmd_kind_exits_two(run_run_cli, tmp_path: Path) -> None:
    """Any kind other than cmd is a deferred feature — clear message, exit 2."""
    # Shim the installed-extension lookup to return our fake extension.
    _write_fake_ext_with_cmd(tmp_path / "fake", "fake.commands.hello:main")
    with patch(
        "axiom.cli.ext.commands.run._installed_extension_path",
        return_value=tmp_path / "fake",
    ):
        rc, out = run_run_cli("fake.agent.some_agent")
    assert rc == 2
    assert "v0.2" in out or "not yet" in out.lower() or "kind=" in out.lower()


def test_bare_ext_picks_sole_cmd(tmp_path: Path, monkeypatch, run_run_cli) -> None:
    """Unit 9: `axi ext run <ext>` picks the only cmd when one is declared."""
    _write_fake_ext_with_cmd(tmp_path / "fake", "run_ext_module2:main")
    ext_pkg = tmp_path / "fake" / "run_ext_module2.py"
    ext_pkg.write_text(
        "CALLS = []\n"
        "def main(args):\n"
        "    CALLS.append(list(args))\n"
        "    return 0\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "fake"))

    with patch(
        "axiom.cli.ext.commands.run._installed_extension_path",
        return_value=tmp_path / "fake",
    ):
        rc, out = run_run_cli("fake")  # bare form!
    assert rc == 0
    import importlib

    mod = importlib.import_module("run_ext_module2")
    assert mod.CALLS == [[]]


def test_bare_ext_multi_cmd_errors_with_list(
    tmp_path: Path, monkeypatch, run_run_cli
) -> None:
    """Unit 9: multi-cmd ext prints the list so user can pick."""
    ext_dir = tmp_path / "multi"
    ext_dir.mkdir()
    (ext_dir / "axiom-extension.toml").write_text(
        "[extension]\n"
        'name = "multi"\n'
        'version = "0.1.0"\n'
        'description = "multi"\n'
        'license = "Apache-2.0"\n'
        'aeos_version = "0.1.0"\n\n'
        "[[extension.provides]]\n"
        'kind = "cmd"\n'
        'noun = "alpha"\n'
        'entry = "multi.commands.alpha:main"\n\n'
        "[[extension.provides]]\n"
        'kind = "cmd"\n'
        'noun = "beta"\n'
        'entry = "multi.commands.beta:main"\n'
    )
    with patch(
        "axiom.cli.ext.commands.run._installed_extension_path",
        return_value=ext_dir,
    ):
        rc, out = run_run_cli("multi")
    assert rc == 1
    assert "multi.cmd.alpha" in out
    assert "multi.cmd.beta" in out


def test_bare_ext_no_cmd_errors(
    tmp_path: Path, monkeypatch, run_run_cli
) -> None:
    """Unit 9: zero-cmd ext says `no runnable cmd`."""
    ext_dir = tmp_path / "nocmd"
    ext_dir.mkdir()
    (ext_dir / "axiom-extension.toml").write_text(
        "[extension]\n"
        'name = "nocmd"\n'
        'version = "0.1.0"\n'
        'description = "nocmd"\n'
        'license = "Apache-2.0"\n'
        'aeos_version = "0.1.0"\n'
    )
    with patch(
        "axiom.cli.ext.commands.run._installed_extension_path",
        return_value=ext_dir,
    ):
        rc, out = run_run_cli("nocmd")
    assert rc == 1
    assert "no runnable cmd" in out.lower()


def test_run_cmd_invokes_entry(tmp_path: Path, monkeypatch, run_run_cli) -> None:
    """End-to-end: a real cmd entry is imported and called with args."""
    # Build a minimal on-disk Python package that exposes `main(args)`.
    tmp_path / "ext_pkg"
    _write_fake_ext_with_cmd(tmp_path / "fake", "run_ext_module:main")
    ext_pkg = tmp_path / "fake" / "run_ext_module.py"
    ext_pkg.write_text(
        "CALLS = []\n"
        "def main(args):\n"
        "    CALLS.append(list(args))\n"
        "    return 0\n"
    )
    # Make the fake module importable for the in-process call.
    monkeypatch.syspath_prepend(str(tmp_path / "fake"))

    with patch(
        "axiom.cli.ext.commands.run._installed_extension_path",
        return_value=tmp_path / "fake",
    ):
        rc, out = run_run_cli("fake.cmd.hello", "--greet", "world")
    assert rc == 0
    # The module's CALLS ledger records our argv passthrough.
    import importlib

    mod = importlib.import_module("run_ext_module")
    assert mod.CALLS == [["--greet", "world"]]
