# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `axi directive` CLI."""

from __future__ import annotations

from axiom.extensions.builtins.directive.cli import main


def test_add_list_revoke_roundtrip(tmp_path, monkeypatch, capsys):
    store = tmp_path / "directives.jsonl"
    monkeypatch.setattr(
        "axiom.policy.version_directive_store._default_store_path",
        lambda: store,
    )

    # add
    rc = main(
        [
            "add",
            "--package",
            "axiom-os-lm",
            "--min-version",
            "0.10.0",
            "--issuer",
            "@ben.booth:axiom",
            "--reason",
            "security patch",
        ]
    )
    assert rc == 0
    assert "Added directive" in capsys.readouterr().out

    # list
    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "axiom-os-lm" in out
    assert "0.10.0" in out

    # grab id from json mode
    rc = main(["--json", "list"])
    import json as _json

    records = _json.loads(capsys.readouterr().out)
    assert len(records) == 1
    did = records[0]["id"]

    # revoke
    rc = main(["revoke", did])
    assert rc == 0
    assert "Revoked" in capsys.readouterr().out

    # active-only list is empty
    rc = main(["list", "--active"])
    assert rc == 0
    assert "No active directives" in capsys.readouterr().out


def test_revoke_unknown_returns_error(tmp_path, monkeypatch, capsys):
    store = tmp_path / "directives.jsonl"
    monkeypatch.setattr(
        "axiom.policy.version_directive_store._default_store_path",
        lambda: store,
    )
    rc = main(["revoke", "nonexistent"])
    assert rc == 1
