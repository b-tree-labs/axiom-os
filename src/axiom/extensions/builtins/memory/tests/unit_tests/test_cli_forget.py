# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axi memory forget` — the CLI redaction subcommand.

Thin wrapper over the memory `forget` skill; drives cli.main() in-process
against an isolated composition (the repo's dominant CLI-test convention).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


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


def _record(cli, principal, summary):
    rc = cli.main([
        "record", "--principal", principal, "--tool", "claude-code",
        "--user-input", f"in {summary}", "--assistant-output", f"out {summary}",
        "--summary", summary,
    ])
    assert rc == 0


def _summaries(composition, principal):
    from axiom.memory.session_summary import list_fragments_by_principal
    return [
        f.content.get("summary", "")
        for f in list_fragments_by_principal(composition, principal, limit=50)
    ]


def test_forget_by_match_removes_only_matching(patched_cli, capsys):
    from axiom.extensions.builtins.memory import cli

    _record(cli, "p@x", "KEEPER note")
    _record(cli, "p@x", "FORGETME residue")
    capsys.readouterr()

    rc = cli.main(["forget", "--principal", "p@x", "--match", "FORGETME"])
    assert rc == 0

    summaries = _summaries(patched_cli, "p@x")
    assert any("KEEPER" in s for s in summaries)
    assert not any("FORGETME" in s for s in summaries)


def test_forget_dry_run_deletes_nothing(patched_cli, capsys):
    from axiom.extensions.builtins.memory import cli

    _record(cli, "p@x", "DRYRUN marker")
    capsys.readouterr()

    rc = cli.main(["forget", "--principal", "p@x", "--match", "DRYRUN", "--dry-run"])
    assert rc == 0
    assert len(_summaries(patched_cli, "p@x")) == 1  # still present


def test_forget_refuses_principal_bulk_without_guard(patched_cli, capsys):
    from axiom.extensions.builtins.memory import cli

    _record(cli, "p@x", "SAFE")
    capsys.readouterr()

    # no --match and no --all → refuse to nuke the whole principal
    rc = cli.main(["forget", "--principal", "p@x"])
    assert rc == 1
    assert len(_summaries(patched_cli, "p@x")) == 1  # untouched


def test_forget_all_purges_principal(patched_cli, capsys):
    from axiom.extensions.builtins.memory import cli

    _record(cli, "p@x", "one")
    _record(cli, "p@x", "two")
    capsys.readouterr()

    rc = cli.main(["forget", "--principal", "p@x", "--all"])
    assert rc == 0
    assert _summaries(patched_cli, "p@x") == []


def test_forget_json_output(patched_cli, capsys):
    from axiom.extensions.builtins.memory import cli

    _record(cli, "p@x", "J")
    capsys.readouterr()

    rc = cli.main(["forget", "--principal", "p@x", "--all", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["forgotten"]
