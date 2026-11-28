# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An extension that is off makes every function and flag it owns resolve off,
on the CLI, over MCP, over HTTP (both faces) and in the resolver appkit's slice
reads (ADR-254 §3)."""

from __future__ import annotations

import json

from axiom.extensions.builtins.features import cli
from axiom.extensions.builtins.features.resolver import EXTENSION_OFF
from axiom.extensions.builtins.features.tests.conftest import mcp, web
from axiom.extensions.builtins.flags import cli as flags_cli


def _json(capsys, *argv, noun=None) -> dict:
    capsys.readouterr()  # drain what earlier verbs printed
    assert (noun or cli).main(["--json", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_the_cli_resolves_the_flag_off_while_its_extension_is_off(switchboard, capsys):
    value = _json(capsys, "resolve", "panel", noun=flags_cli)
    assert value["flags"] == {"panel": "off"} and value["sources"] == {"panel": EXTENSION_OFF}
    assert {r["name"]: r for r in _json(capsys, "list", noun=flags_cli)["rows"]}["panel"]["details"]["owner_off"]
    rows = {r["name"]: r for r in _json(capsys)["rows"]}
    assert (rows["widget"]["enabled"], rows["widget"]["source"]) == (False, EXTENSION_OFF)
    # Turning the extension on lets its flag (default on) and command through.
    assert cli.main(["enable", "widgets"]) == 0
    value = _json(capsys, "resolve", "panel", noun=flags_cli)
    assert value["flags"] == {"panel": "on"} and value["sources"] == {"panel": "default"}
    rows = {r["name"]: r for r in _json(capsys)["rows"]}
    assert rows["widget"]["enabled"] and rows["widget"]["source"] == "always"  # an unconfined node shows it


def test_the_features_noun_points_at_flags_to_resolve(switchboard, capsys):
    capsys.readouterr()
    assert cli.main(["resolve", "panel"]) == 2
    assert "flags resolve" in capsys.readouterr().err


def test_mcp_resolves_the_same_under_both_tool_names(switchboard):
    state = switchboard["state"]
    for tool in ("axiom_features__resolve", "axiom_flags__resolve"):
        out = mcp(tool, {"key": "panel"}, state)
        assert out["ok"], out
        assert out["value"]["flags"] == {"panel": "off"} and out["value"]["sources"] == {"panel": EXTENSION_OFF}
    on = mcp("axiom_features__enable", {"name": "widgets"}, state)
    assert on["ok"], on
    assert mcp("axiom_flags__resolve", {"key": "panel"}, state)["value"]["flags"] == {"panel": "on"}


def test_http_resolves_the_same_on_the_switchboard_and_the_flags_alias(switchboard, monkeypatch):
    client = web(monkeypatch)
    board = client.get("/api/v1/features")
    assert board.status_code == 200, board.text
    body = board.json()
    assert body["flags"]["panel"] == "off" and body["sources"]["panel"] == EXTENSION_OFF
    switches = {(r["kind"], r["name"]): r for r in body["switches"]}
    assert switches[("extension", "widgets")]["enabled"] is False
    assert switches[("function", "widget")]["source"] == EXTENSION_OFF
    assert switches[("flag", "panel")]["details"]["owner_off"] is True

    flags = client.get("/api/v1/flags").json()  # the slice appkit reads today
    assert flags["flags"] == body["flags"] and flags["sources"] == body["sources"]
    assert set(flags["metadata"]["panel"]) == {"description", "default", "protected", "owner"}
    assert "switches" not in flags


def test_the_resolver_alone_applies_the_rule_for_in_process_readers(switchboard):
    """``FlagService().is_enabled`` is what a module gating itself on a flag calls."""
    from axiom.extensions.builtins.features.resolver import FlagService

    assert FlagService().is_enabled("panel", node="node-a") is False
    assert cli.main(["enable", "widgets"]) == 0
    assert FlagService().is_enabled("panel", node="node-a") is True
