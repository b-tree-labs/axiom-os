# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi connector reload [<name>]``, as typed, reaches the connector extension's reload.

``connect`` (Registry Fabric, ADR-074) and ``connector`` both claim the
``connector`` noun, and ``connect`` wins (tests/extensions/test_cli_noun_collisions.py),
so none of the connector extension's own verbs could be typed. ``reload`` is the
one ADR-220 §3 and prd-video VID-015 depend on: it applies changed configuration
(a storage connector, a video source's triggers) without a restart. Until the noun
has one owner (docs/working/goal-connector-cohesion-2026-09-14.md, Follow-ons),
connect's CLI hands ``reload`` to the connector extension's CLI, which calls skill
``connector.reload``: one skill, one implementation, one output (ADR-056).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.connector import configured


def _row(name, health="ready", detail="applied to the running capture: motion"):
    return {"connector": name, "kind": "gizmo", "health": health, "detail": detail}


class _Storage:
    """Behaves like the real storage manager: it mounts what is configured, and reports
    ANY name it is asked to reload as unmounted, "no longer configured"."""

    asked: list = []

    def __init__(self, *a, **k):
        pass

    def names(self):
        return ["share"]

    def reload(self, name=None):
        _Storage.asked.append(name)
        names = ["share"] if name is None else [name]
        return {
            n: SimpleNamespace(
                name=n,
                health=None,
                reason="no longer configured" if n != "share" else "ready",
                as_dict=lambda n=n: {
                    "name": n,
                    "kind": "local",
                    "health": "unmounted" if n != "share" else "ready",
                    "reason": "no longer configured" if n != "share" else "mounted",
                },
            )
            for n in names
        }

    def role_status(self):
        return {}


@pytest.fixture
def node(monkeypatch, tmp_path):
    from axiom.extensions.builtins.connector.storage import manager

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(manager, "StorageConnectorManager", _Storage)
    _Storage.asked = []
    rows = {"cam-1": _row("cam-1")}
    monkeypatch.setattr(
        configured,
        "reloaders",
        lambda **kw: [
            ("fam", lambda s, n: [rows[n]] if n in rows else ([] if n else list(rows.values())))
        ],
    )
    return rows


def _main(argv):
    from axiom.extensions.builtins.connect.connector_cli import main

    return main(argv)


def test_reload_is_a_verb_of_the_connector_command():
    from axiom.extensions.builtins.connect.connector_cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["reload"]).verb == "reload"
    assert parser.parse_args(["reload", "cam-1"]).connector == "cam-1"
    assert "reload" in parser.format_help()


def test_reload_of_a_family_connector_applies_it(node, capsys):
    assert _main(["reload", "cam-1"]) == 0
    out = capsys.readouterr().out
    assert "cam-1" in out and "applied to the running capture" in out
    assert "no longer configured" not in out
    assert _Storage.asked == [], "storage is not asked about a name it does not have"


def test_reload_json_is_the_connector_extensions_envelope(node, capsys):
    assert _main(["--json", "reload", "cam-1"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert [r["connector"] for r in envelope["value"]["configured"]] == ["cam-1"]


def test_reload_without_a_name_reloads_storage_and_every_family(node, capsys):
    assert _main(["reload"]) == 0
    out = capsys.readouterr().out
    assert "share" in out and "cam-1" in out
    assert _Storage.asked == [None]


def test_reload_of_a_storage_connector_still_reaches_storage(node, capsys):
    assert _main(["reload", "share"]) == 0
    assert _Storage.asked == ["share"]


def test_reload_of_a_name_nobody_has_is_an_error_not_no_longer_configured(node, capsys):
    """A typo must not read as a storage connector that went away."""
    assert _main(["reload", "cma-1"]) == 1
    captured = capsys.readouterr()
    assert "cma-1" in captured.err and "no longer configured" not in captured.out + captured.err
    assert "Traceback" not in captured.err
