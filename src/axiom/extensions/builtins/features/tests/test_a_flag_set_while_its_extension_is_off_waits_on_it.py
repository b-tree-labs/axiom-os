# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A flag whose extension is off may be set, and waits on that extension (ADR-254 D3).

Owner decision 3 of 2026-11-12, replacing the refusal (HTTP 409): the value is
stored, still resolves ``off`` with source ``extension-off``, and every surface
(CLI, MCP, HTTP ``/features`` and ``/flags``, the in-process resolver appkit's
slice reads) says it is waiting on that extension. When the extension turns on,
the stored value takes effect at once. This lets a rollout be staged before an
extension is turned on. A command of an off extension is still refused by its
own name: only flag values change.

``panel`` is declared ``default = "on"`` by ``widgets``, which is off; setting it
``off`` is how these tests tell the stored value from the default.
"""

from __future__ import annotations

import json

from axiom.extensions.builtins.features import cli
from axiom.extensions.builtins.features.resolver import EXTENSION_OFF, FlagService
from axiom.extensions.builtins.features.tests.conftest import mcp, web
from axiom.extensions.builtins.flags import cli as flags_cli

WAITING = "waiting on extension widgets"


def _json(capsys, *argv, noun=None) -> dict:
    capsys.readouterr()
    assert (noun or flags_cli).main(["--json", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def _stored(switchboard) -> dict:
    return {(r["scope"], r["scope_id"]): r["value"] for r in switchboard["store"].rows("panel")}


def test_the_cli_sets_it_says_it_waits_and_the_value_applies_when_the_extension_turns_on(switchboard, capsys):
    capsys.readouterr()
    assert flags_cli.main(["set", "panel", "off"]) == 0
    out = capsys.readouterr().out
    assert WAITING in out
    assert _stored(switchboard) == {("node", "node-a"): "off"}

    value = _json(capsys, "resolve", "panel")
    assert value["flags"] == {"panel": "off"} and value["sources"] == {"panel": EXTENSION_OFF}
    assert value["waiting_on"] == {"panel": "widgets"}
    assert value["staged"] == {"panel": {"value": "off", "source": "node"}}
    capsys.readouterr()
    assert flags_cli.main(["resolve", "panel"]) == 0
    assert WAITING in capsys.readouterr().out

    rows = {r["name"]: r for r in _json(capsys, "list")["rows"]}
    details = rows["panel"]["details"]
    assert (rows["panel"]["value"], rows["panel"]["source"]) == ("off", EXTENSION_OFF)
    assert details["waiting_on"] == "widgets"
    assert (details["staged_value"], details["staged_source"]) == ("off", "node")
    capsys.readouterr()
    assert flags_cli.main(["list"]) == 0
    assert WAITING in capsys.readouterr().out

    assert cli.main(["enable", "widgets"]) == 0
    value = _json(capsys, "resolve", "panel")
    assert value["flags"] == {"panel": "off"} and value["sources"] == {"panel": "node"}
    assert value["waiting_on"] == {} and value["staged"] == {}


def test_the_flags_noun_says_it_waits_in_its_json_too(switchboard, capsys):
    capsys.readouterr()
    assert flags_cli.main(["set", "panel", "off"]) == 0
    assert WAITING in capsys.readouterr().out
    assert flags_cli.main(["--json", "resolve", "panel"]) == 0
    assert json.loads(capsys.readouterr().out)["waiting_on"] == {"panel": "widgets"}


def test_mcp_sets_it_and_says_it_waits(switchboard):
    state = switchboard["state"]
    out = mcp("axiom_flags__set", {"key": "panel", "value": "off"}, state)
    assert out["ok"], out
    assert out["value"]["waiting_on"] == "widgets"
    assert any(WAITING in a for a in out.get("actions_taken") or []), out
    out = mcp("axiom_flags__set", {"key": "panel", "value": "off", "scope": "account", "scope_id": "acct"}, state)
    assert out["ok"], out
    assert out["value"]["waiting_on"] == "widgets"
    resolved = mcp("axiom_flags__resolve", {"key": "panel"}, state)["value"]
    assert resolved["sources"] == {"panel": EXTENSION_OFF} and resolved["waiting_on"] == {"panel": "widgets"}

    assert mcp("axiom_features__enable", {"name": "widgets"}, state)["ok"]
    resolved = mcp("axiom_flags__resolve", {"key": "panel", "account": "acct"}, state)["value"]
    assert resolved["flags"] == {"panel": "off"} and resolved["sources"] == {"panel": "account"}


def test_http_sets_it_on_both_faces_and_both_say_it_waits(switchboard, monkeypatch):
    operator = web(monkeypatch, roles=["site-admin"])
    r = operator.patch("/api/v1/features/panel", json={"value": "off", "scope": "node"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["set"]["waiting_on"] == "widgets"
    assert body["flags"]["panel"] == "off" and body["sources"]["panel"] == EXTENSION_OFF
    assert body["waiting_on"] == {"panel": "widgets"}
    (row,) = [s for s in body["switches"] if (s["kind"], s["name"]) == ("flag", "panel")]
    assert row["details"]["waiting_on"] == "widgets" and row["details"]["staged_value"] == "off"

    r = web(monkeypatch).patch("/api/v1/flags/panel", json={"value": "off"})
    assert r.status_code == 200, r.text
    flags = web(monkeypatch).get("/api/v1/flags").json()
    assert flags["sources"]["panel"] == EXTENSION_OFF and flags["waiting_on"] == {"panel": "widgets"}
    assert set(flags["metadata"]["panel"]) == {"description", "default", "protected", "owner"}, (
        "the alias keeps its metadata shape; waiting_on is a new top-level key"
    )
    assert _stored(switchboard) == {("node", "node-a"): "off", ("user", "u9"): "off"}

    assert cli.main(["enable", "widgets"]) == 0
    flags = web(monkeypatch).get("/api/v1/flags").json()
    assert flags["flags"]["panel"] == "off" and flags["sources"]["panel"] == "user"
    assert flags["waiting_on"] == {}


def test_the_in_process_resolver_says_what_waits_and_applies_it_at_once(switchboard):
    """``FlagService().resolve`` is what appkit's slice and an in-process gate read."""
    svc = FlagService()
    svc.set_override("node", "node-a", "panel", "off", by="@t")
    res = svc.resolve(node="node-a")
    assert (res.flags["panel"], res.sources["panel"]) == ("off", EXTENSION_OFF)
    assert res.waiting_on == {"panel": "widgets"}
    assert res.staged == {"panel": ("off", "node")}
    assert svc.get_flag_metadata()["panel"]["waiting_on"] == "widgets"
    assert cli.main(["enable", "widgets"]) == 0
    res = FlagService().resolve(node="node-a")
    assert (res.flags["panel"], res.sources["panel"]) == ("off", "node")
    assert res.waiting_on == {}


def test_a_command_of_an_off_extension_is_still_refused_by_its_own_name(switchboard, capsys):
    capsys.readouterr()
    assert cli.main(["enable", "widget"]) == 1
    assert "features enable widgets" in capsys.readouterr().err
