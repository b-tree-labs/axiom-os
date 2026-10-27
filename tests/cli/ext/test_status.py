# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext status``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from axiom.cli.ext.commands.status import StatusProvider, build_dashboard
from axiom.cli.ext.provider import CliContext


def _run(tmp_path: Path, *argv: str) -> int:
    provider = StatusProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    return provider.run(args, CliContext(cwd=tmp_path))


class TestWelcome:
    """Empty state → welcome block with the lifecycle entry points."""

    def test_empty_home_shows_welcome(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("AXIOM_HOME", str(tmp_path / "does-not-exist"))
        # Also ensure the install state file doesn't leak in from the real
        # user home: list_installed already honors AXIOM_HOME.
        assert _run(tmp_path) == 0
        out = capsys.readouterr().out
        assert "Welcome" in out or "welcome" in out.lower()
        assert "axi ext init" in out
        assert "axi ext search" in out

    def test_welcome_mode_in_dashboard_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("AXIOM_HOME", str(tmp_path / "does-not-exist"))
        d = build_dashboard()
        assert d.mode == "welcome"


class TestDashboard:
    """When state exists, dashboard shows counts + registry + publisher key."""

    def test_dashboard_after_sign(
        self, tmp_path: Path, monkeypatch, capsys, scaffolded_extension
    ) -> None:
        monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))

        ext = scaffolded_extension("dash_ext")
        from axiom.cli.ext.commands.sign import SignProvider

        sp = SignProvider()
        parser = argparse.ArgumentParser()
        sp.add_arguments(parser)
        assert sp.run(
            parser.parse_args([str(ext), "--yes"]), CliContext(cwd=tmp_path)
        ) == 0
        capsys.readouterr()

        assert _run(tmp_path) == 0
        out = capsys.readouterr().out
        assert "axi ext status" in out.lower()
        assert "registry" in out
        assert "publisher key:" in out


class TestJson:
    """`--json` emits structured output with all fields."""

    def test_json_shape(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AXIOM_HOME", str(tmp_path / "does-not-exist"))
        assert _run(tmp_path, "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        for key in (
            "mode",
            "axiom_home",
            "axiom_home_exists",
            "publisher_key_fingerprint",
            "registry_url",
            "registry_entry_count",
            "registry_latest_publish",
            "installed_axi",
            "installed_pip",
        ):
            assert key in payload


def test_status_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "status" in providers
