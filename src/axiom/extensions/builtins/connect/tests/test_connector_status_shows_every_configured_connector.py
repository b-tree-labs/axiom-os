# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi connector status``, the command a person types, lists every configured connector.

``connect`` (Registry Fabric) and ``connector`` both claim the ``connector`` noun,
and ``connect`` wins (tests/extensions/test_cli_noun_collisions.py). So the status
a person reaches is this one, and it read only the fabric's in-process connection
store: a fresh process always said "No connections yet" on a node with declared
connectors, and ``status <name>`` for any name not in that store crashed on
``None``. It now also asks every connector family (``connector.configured``), the
same rows ``connector.status`` returns, and names no kind.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.connector import configured
from axiom.infra import branding


def _row(name, health="ready", detail="running, 5 fps"):
    return {"connector": name, "kind": "gizmo", "health": health, "detail": detail}


@pytest.fixture
def families(monkeypatch, tmp_path):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    rows = [_row("cam-1"), _row("cam-2", health="degraded", detail="reconnecting")]
    monkeypatch.setattr(configured, "providers", lambda **kw: [("fam", lambda s: list(rows))])
    return rows


@pytest.fixture
def no_families(monkeypatch, tmp_path):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(configured, "providers", lambda **kw: [])


def _main(argv):
    from axiom.extensions.builtins.connect.connector_cli import main

    return main(argv)


def test_the_skill_returns_configured_connectors_beside_connections(families):
    from axiom.extensions.builtins.connect.skills import registry_ops

    value = registry_ops.status({}).value
    assert value["connections"] == []
    assert [r["connector"] for r in value["configured"]] == ["cam-1", "cam-2"]
    assert value["configured_problems"] == []


def test_status_lists_them(families, capsys):
    assert _main(["status"]) == 0
    out = capsys.readouterr().out
    assert "cam-1" in out and "cam-2" in out and "reconnecting" in out and "gizmo" in out
    assert "No connections yet" not in out, (
        "a node with configured connectors is not told it has none"
    )


def test_status_json_carries_them(families, capsys):
    assert _main(["--json", "status"]) == 0
    assert [r["connector"] for r in json.loads(capsys.readouterr().out)["configured"]] == [
        "cam-1",
        "cam-2",
    ]


def test_status_of_one_configured_connector(families, capsys):
    assert _main(["status", "cam-2"]) == 0
    out = capsys.readouterr().out
    assert "cam-2" in out and "cam-1" not in out


def test_status_of_a_name_nobody_has_says_so_instead_of_crashing(families, capsys):
    assert _main(["status", "ghost"]) == 1
    captured = capsys.readouterr()
    assert "ghost" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_with_nothing_anywhere_the_hint_names_the_cli_in_use(no_families, capsys):
    branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
    try:
        assert _main(["status"]) == 0
    finally:
        branding.reset()
    out = capsys.readouterr().out
    assert "acme connector install" in out and "axi connector" not in out
