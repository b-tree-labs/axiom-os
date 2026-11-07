# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext whoami``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from axiom.cli.ext.commands.whoami import WhoamiProvider, build_summary
from axiom.cli.ext.provider import CliContext


def _run(tmp_path: Path, *argv: str) -> int:
    provider = WhoamiProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(list(argv))
    return provider.run(args, CliContext(cwd=tmp_path))


class TestPristine:
    """No publisher key, no installs — output explains what to do next."""

    def test_reports_no_key(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))
        assert _run(tmp_path) == 0
        out = capsys.readouterr().out
        assert "$AXIOM_HOME" in out
        assert "registry:" in out
        assert "not yet created" in out
        assert "axi ext sign" in out
        assert "0 axi-managed" in out

    def test_json_shape(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))
        assert _run(tmp_path, "--json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert "axiom_home" in payload
        assert "registry_url" in payload
        assert "publisher_key_fingerprint" in payload
        assert "installed_axi" in payload
        assert "installed_pip" in payload
        assert "trusted_publishers" in payload
        assert payload["publisher_key_fingerprint"] == ""


class TestAfterSign:
    """After `axi ext sign`, whoami reports the fingerprint."""

    def test_fingerprint_appears_after_keygen(
        self, tmp_path: Path, monkeypatch, capsys, scaffolded_extension
    ) -> None:
        monkeypatch.setenv("AXIOM_HOME", str(tmp_path / ".axiom"))
        ext = scaffolded_extension("kp_ext")

        from axiom.cli.ext.commands.sign import SignProvider

        p = SignProvider()
        parser = argparse.ArgumentParser()
        p.add_arguments(parser)
        assert p.run(
            parser.parse_args([str(ext), "--yes"]),
            CliContext(cwd=tmp_path),
        ) == 0
        capsys.readouterr()

        assert _run(tmp_path) == 0
        out = capsys.readouterr().out
        assert "publisher key:" in out
        # 64-char hex fingerprint
        summary = build_summary()
        assert summary.publisher_key_fingerprint
        assert summary.publisher_key_fingerprint in out


class TestRegistryOverride:
    """When ``AXIOM_REGISTRY_URL`` is set, the source label reflects it."""

    def test_env_override_labeled(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        axiom_home = tmp_path / ".axiom"
        monkeypatch.setenv("AXIOM_HOME", str(axiom_home))
        reg = tmp_path / "alt-registry"
        reg.mkdir()
        monkeypatch.setenv("AXIOM_REGISTRY_URL", f"file://{reg}")
        assert _run(tmp_path) == 0
        out = capsys.readouterr().out
        assert str(reg) in out
        assert "AXIOM_REGISTRY_URL" in out


def test_whoami_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "whoami" in providers
