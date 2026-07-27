# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi memory export`` / ``axi memory import`` — thin ADR-056
wrappers over the export/import bundle skills.

In-process tests drive cli.main() against an isolated composition (the
repo's dominant CLI-test convention); the end-to-end subprocess smokes
live in test_cli_export_import_smoke.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PRINCIPAL = "@alice:personal"
ASSUMED = "@alice:work"


@pytest.fixture
def isolated_composition(tmp_path: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    base = tmp_path / "memory"
    base.mkdir()
    kp = generate_keypair()
    reg = ArtifactRegistry(backend=SQLiteBackend(base / "artifacts.db"))
    audit = AuditLog(base / "audit.jsonl", signing_keypair=kp)
    return CompositionService(
        artifact_registry=reg,
        audit_log=audit,
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


@pytest.fixture
def patched_cli(monkeypatch, isolated_composition):
    from axiom.extensions.builtins.memory import cli

    monkeypatch.setattr(
        cli, "_build_default_composition", lambda: isolated_composition,
    )
    return isolated_composition


def _record(cli, principal: str, summary: str) -> None:
    rc = cli.main([
        "record", "--principal", principal, "--tool", "claude-code",
        "--user-input", f"in {summary}", "--assistant-output", f"out {summary}",
        "--summary", summary,
    ])
    assert rc == 0


class TestCliExport:
    def test_export_writes_bundle(self, patched_cli, tmp_path, capsys):
        from axiom.extensions.builtins.memory import cli

        _record(cli, PRINCIPAL, "first")
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        out = tmp_path / "bundle.tar.gz"
        capsys.readouterr()
        rc = cli.main([
            "export", "--principal", PRINCIPAL, "--out", str(out),
            "--sessions-dir", str(sessions), "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["fragments"] == 1
        assert out.exists()

    def test_include_vault_refused(self, patched_cli, tmp_path, capsys):
        from axiom.extensions.builtins.memory import cli

        _record(cli, PRINCIPAL, "first")
        out = tmp_path / "bundle.tar.gz"
        rc = cli.main([
            "export", "--principal", PRINCIPAL, "--out", str(out),
            "--include-vault",
        ])
        assert rc == 1
        assert "vault" in capsys.readouterr().err.lower()
        assert not out.exists()


class TestCliImport:
    def test_round_trip(self, patched_cli, tmp_path, capsys):
        from axiom.extensions.builtins.memory import cli

        _record(cli, PRINCIPAL, "first")
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        out = tmp_path / "bundle.tar.gz"
        assert cli.main([
            "export", "--principal", PRINCIPAL, "--out", str(out),
            "--sessions-dir", str(sessions),
        ]) == 0
        capsys.readouterr()

        rc = cli.main([
            "import", str(out), "--assume-principal", ASSUMED,
            "--sessions-dir", str(tmp_path / "dst-sessions"), "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        # The bundle re-imports into the same store: the fragment already
        # exists with identical content, so the exact dedup tier skips it.
        assert payload["imported"] == 0
        assert payload["skipped_duplicate"] == 1

    def test_import_dry_run(self, patched_cli, tmp_path, capsys):
        from axiom.extensions.builtins.memory import cli

        _record(cli, PRINCIPAL, "first")
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        out = tmp_path / "bundle.tar.gz"
        assert cli.main([
            "export", "--principal", PRINCIPAL, "--out", str(out),
            "--sessions-dir", str(sessions),
        ]) == 0
        capsys.readouterr()

        rc = cli.main([
            "import", str(out), "--assume-principal", ASSUMED,
            "--dry-run", "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True

    def test_import_missing_bundle_fails(self, patched_cli, tmp_path, capsys):
        from axiom.extensions.builtins.memory import cli

        rc = cli.main([
            "import", str(tmp_path / "nope.tar.gz"),
            "--assume-principal", ASSUMED,
        ])
        assert rc == 1
