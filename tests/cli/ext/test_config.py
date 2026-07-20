# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext config`` — per-extension key/value config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from axiom.cli.ext.commands.config import ConfigProvider, _config_path_for
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    return home


@pytest.fixture
def run_config_cli(capsys):
    def _run(*argv: str, cwd: Path | None = None) -> tuple[int, str]:
        capsys.readouterr()
        provider = ConfigProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args(list(argv))
        ctx = CliContext(cwd=cwd or Path.cwd())
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_config_provider_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "config" in providers


def test_config_path_for_honors_axiom_home(axiom_home: Path) -> None:
    path = _config_path_for("foo")
    assert path.parent == axiom_home / "config"
    assert path.name == "foo.json"


# ---------------------------------------------------------------------------
# set / get / list / unset
# ---------------------------------------------------------------------------


def test_config_set_then_get(axiom_home: Path, run_config_cli) -> None:
    rc, _ = run_config_cli("foo", "set", "canvas_url", "https://example.com")
    assert rc == 0
    rc, out = run_config_cli("foo", "get", "canvas_url")
    assert rc == 0
    assert "https://example.com" in out


def test_config_list_dumps_keys(axiom_home: Path, run_config_cli) -> None:
    run_config_cli("foo", "set", "a", "1")
    run_config_cli("foo", "set", "b", "2")
    rc, out = run_config_cli("foo", "list")
    assert rc == 0
    assert "a" in out and "b" in out
    assert "1" in out and "2" in out


def test_config_list_json_is_parseable(axiom_home: Path, run_config_cli) -> None:
    run_config_cli("foo", "set", "x", "y")
    rc, out = run_config_cli("foo", "list", "--json")
    assert rc == 0
    data = json.loads(out)
    assert data == {"x": "y"}


def test_config_unset_removes_key(axiom_home: Path, run_config_cli) -> None:
    run_config_cli("foo", "set", "k", "v")
    rc, _ = run_config_cli("foo", "unset", "k")
    assert rc == 0
    rc, out = run_config_cli("foo", "list")
    assert rc == 0
    assert "k" not in out or '"k":' not in out


def test_config_get_unknown_key_returns_non_zero(
    axiom_home: Path, run_config_cli
) -> None:
    rc, out = run_config_cli("foo", "get", "never_set")
    assert rc != 0


def test_config_unset_unknown_key_is_tolerant(
    axiom_home: Path, run_config_cli
) -> None:
    # Idempotent unset: unsetting a missing key must not error.
    rc, _ = run_config_cli("foo", "unset", "phantom")
    assert rc == 0


# ---------------------------------------------------------------------------
# Ext-name resolution from cwd
# ---------------------------------------------------------------------------


def test_config_resolves_ext_name_from_cwd(
    scaffolded_extension, axiom_home: Path, run_config_cli
) -> None:
    ext = scaffolded_extension("resolved_ext")
    rc, _ = run_config_cli("set", "origin", "cwd", cwd=ext)
    assert rc == 0
    rc, out = run_config_cli("get", "origin", cwd=ext)
    assert rc == 0
    assert "cwd" in out
    # And the backing file must be keyed by the manifest's name.
    assert (axiom_home / "config" / "resolved_ext.json").exists()


def test_config_errors_when_ext_name_cannot_be_resolved(
    tmp_path: Path, axiom_home: Path, run_config_cli
) -> None:
    # Not an extension; no manifest — and no explicit ext name.
    rc, out = run_config_cli("list", cwd=tmp_path)
    assert rc != 0


# ---------------------------------------------------------------------------
# File format: JSON, human-readable
# ---------------------------------------------------------------------------


def test_config_set_writes_valid_json(axiom_home: Path, run_config_cli) -> None:
    run_config_cli("foo", "set", "a", "1")
    run_config_cli("foo", "set", "b", "two")
    data = json.loads((axiom_home / "config" / "foo.json").read_text())
    assert data == {"a": "1", "b": "two"}
