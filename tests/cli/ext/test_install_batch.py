# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext install -r <reqs>`` — batch / requirements install."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.install import InstallProvider
from axiom.cli.ext.commands.install_batch import (
    BatchEntry,
    parse_requirements_file,
    resolve_version_spec,
)
from axiom.cli.ext.commands.publish import publish_extension
from axiom.cli.ext.install_state import get_installed
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    monkeypatch.setenv("AXIOM_INSTALL_NO_PIP", "1")
    return home


def _publish_fresh(
    scaffolded_extension,
    name: str,
    version: str = "0.1.0",
    *,
    scaffold_dirname: str | None = None,
) -> Path:
    scaffold_dirname = scaffold_dirname or name
    ext = scaffolded_extension(scaffold_dirname)
    manifest = ext / "axiom-extension.toml"
    text = manifest.read_text()
    text = text.replace(f'name = "{scaffold_dirname}"', f'name = "{name}"')
    text = text.replace('version = "0.1.0"', f'version = "{version}"')
    manifest.write_text(text, encoding="utf-8")
    pyproject = ext / "pyproject.toml"
    py_text = pyproject.read_text()
    py_text = py_text.replace(f'name = "{scaffold_dirname}"', f'name = "{name}"')
    py_text = py_text.replace('version = "0.1.0"', f'version = "{version}"')
    pyproject.write_text(py_text, encoding="utf-8")
    publish_extension(ext, yes=True, skip_tag_check=True)
    return ext


def _run(*argv: str, capsys) -> tuple[int, str, str]:
    provider = InstallProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    ctx = CliContext(cwd=Path.cwd())
    capsys.readouterr()
    rc = provider.run(args, ctx)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# ---------------------------------------------------------------------------
# Requirements parser
# ---------------------------------------------------------------------------


def test_parse_requirements_basic(tmp_path: Path) -> None:
    reqs = tmp_path / "reqs.txt"
    reqs.write_text(
        "# comment line\n"
        "greeter==0.1.0\n"
        "foo\n"
        "bar>=0.2,<1.0  # range\n"
        "\n"  # blank
        "  baz  \n",
        encoding="utf-8",
    )
    parsed = parse_requirements_file(reqs)
    assert parsed == [
        BatchEntry(name="greeter", spec="==0.1.0"),
        BatchEntry(name="foo", spec=""),
        BatchEntry(name="bar", spec=">=0.2,<1.0"),
        BatchEntry(name="baz", spec=""),
    ]


def test_parse_requirements_rejects_bad_line(tmp_path: Path) -> None:
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("a good line\n==dangling==\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        parse_requirements_file(reqs)
    assert "line" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Version spec resolution
# ---------------------------------------------------------------------------


def test_resolve_version_spec_exact() -> None:
    available = ["0.1.0", "0.2.0", "0.3.0"]
    assert resolve_version_spec("==0.2.0", available) == "0.2.0"


def test_resolve_version_spec_ge_lt() -> None:
    available = ["0.1.0", "0.2.0", "0.2.5", "1.0.0"]
    # Pick the highest version that satisfies both bounds.
    assert resolve_version_spec(">=0.2,<1.0", available) == "0.2.5"


def test_resolve_version_spec_empty_picks_latest() -> None:
    available = ["0.1.0", "0.2.0"]
    assert resolve_version_spec("", available) == "0.2.0"


def test_resolve_version_spec_none_match_returns_none() -> None:
    available = ["0.1.0"]
    assert resolve_version_spec(">=9", available) is None


# ---------------------------------------------------------------------------
# CLI: happy path
# ---------------------------------------------------------------------------


def test_install_requirements_happy_path(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "alpha")
    _publish_fresh(scaffolded_extension, "beta")

    reqs = tmp_path / "reqs.txt"
    reqs.write_text("alpha==0.1.0\nbeta\n", encoding="utf-8")

    rc, out, _ = _run("-r", str(reqs), "--no-pip", capsys=capsys)
    assert rc == 0, out
    assert get_installed("alpha") is not None
    assert get_installed("beta") is not None
    # Summary line shows counts.
    assert "installed" in out.lower()


# ---------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------


def test_install_requirements_continues_past_failure(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "alpha")
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("alpha\nnever_published\n", encoding="utf-8")

    rc, out, _ = _run("-r", str(reqs), "--no-pip", capsys=capsys)
    assert rc == 1  # partial failure -> non-zero exit
    assert get_installed("alpha") is not None
    assert get_installed("never_published") is None
    # Batch summary should surface both counts.
    lowered = out.lower()
    assert "installed" in lowered
    assert "fail" in lowered


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_install_requirements_dry_run(
    scaffolded_extension, axiom_home: Path, tmp_path: Path, capsys
) -> None:
    _publish_fresh(scaffolded_extension, "alpha")
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("alpha\n", encoding="utf-8")

    rc, out, _ = _run("-r", str(reqs), "--dry-run", "--no-pip", capsys=capsys)
    assert rc == 0
    assert get_installed("alpha") is None
    assert "dry-run" in out or "dry run" in out.lower()
    assert "alpha" in out


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


def test_install_requirements_missing_file_exits_2(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    rc, out, err = _run(
        "-r", str(tmp_path / "nope.txt"), "--no-pip", capsys=capsys
    )
    assert rc == 2
    assert "nope.txt" in (out + err)


# ---------------------------------------------------------------------------
# Bad line
# ---------------------------------------------------------------------------


def test_install_requirements_bad_line_exits_2(
    axiom_home: Path, tmp_path: Path, capsys
) -> None:
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("a\n==oops==\n", encoding="utf-8")
    rc, out, err = _run("-r", str(reqs), "--no-pip", capsys=capsys)
    assert rc == 2
    assert "line" in (out + err).lower()
