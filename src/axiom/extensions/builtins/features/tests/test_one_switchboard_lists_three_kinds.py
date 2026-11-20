# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One registry, three kinds, each with its scopes (ADR-254 §1-2), under two nouns (amended 2026-11-12):
``features`` lists the extension and function kinds, ``flags`` the flag kind."""

from __future__ import annotations

import json

from axiom.extensions.builtins.features import cli
from axiom.extensions.builtins.flags import cli as flags_cli


def _rows(capsys, *argv, noun=None) -> dict:
    capsys.readouterr()  # drain what earlier verbs printed
    assert (noun or cli).main(["--json", *argv]) == 0
    return json.loads(capsys.readouterr().out)


def test_the_switchboard_lists_every_kind_with_its_owner_and_scopes(switchboard, capsys):
    value = _rows(capsys)
    rows = {(r["kind"], r["name"]): r for r in value["rows"]}
    assert value["kinds"] == ["extension", "function", "flag"]
    assert value["scopes"] == {"extension": ["node"], "function": ["node"], "flag": ["node", "account", "user"]}

    widgets = rows[("extension", "widgets")]
    assert widgets["scopes"] == ["node"] and widgets["owner"] == "widgets" and not widgets["enabled"]

    widget = rows[("function", "widget")]  # a command no function owns: its own row, under its extension
    assert widget["subkind"] == "command" and widget["owner"] == "widgets" and widget["scopes"] == ["node"]
    assert (widget["enabled"], widget["source"]) == (False, "extension-off")
    assistant = rows[("function", "assistant")]  # a function's commands fold into its row
    assert assistant["subkind"] == "node-function" and assistant["commands"] == ["chat"]
    assert rows[("function", "background-agents")]["subkind"] == "service"
    assert rows[("function", "agents")]["subkind"] == "policy" and rows[("function", "agents")]["value"] == "local"

    assert not any(kind == "flag" for kind, _ in rows)  # flags are the flags noun's
    rows = {(r["kind"], r["name"]): r for r in _rows(capsys, "list", noun=flags_cli)["rows"]}
    timeline = rows[("flag", "timeline")]
    assert timeline["owner"] == "core_ext" and timeline["scopes"] == ["node", "account", "user"]
    assert (timeline["value"], timeline["source"]) == ("off", "default")
    assert rows[("flag", "library")]["value"] == "soon"


def test_one_kind_at_a_time(switchboard, capsys):
    for kind in ("extension", "function"):
        value = _rows(capsys, "list", "--kind", kind)
        assert value["rows"] and {r["kind"] for r in value["rows"]} == {kind}
    assert _rows(capsys, "list", "--kind", "flag")["rows"] == []  # a pointer at flags list, no rows
    value = _rows(capsys, "list", noun=flags_cli)
    assert value["rows"] and {r["kind"] for r in value["rows"]} == {"flag"}
    assert flags_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "timeline" in out and "Extensions" not in out


def test_a_flag_is_set_at_any_of_its_scopes(switchboard, capsys):
    assert flags_cli.main(["set", "timeline", "on", "--scope", "user", "--id", "u1"]) == 0
    assert flags_cli.main(["set", "timeline", "soon", "--scope", "account", "--id", "acct"]) == 0
    assert flags_cli.main(["set", "timeline", "off"]) == 0  # node scope, this node
    rows = {(r["scope"], r["scope_id"]): r["value"] for r in switchboard["store"].rows("timeline")}
    assert rows == {("user", "u1"): "on", ("account", "acct"): "soon", ("node", "node-a"): "off"}
    value = _rows(capsys, "resolve", "timeline", "--user", "u1", "--account", "acct", noun=flags_cli)
    assert value["flags"] == {"timeline": "on"} and value["sources"] == {"timeline": "user"}


def test_an_extension_or_function_switch_is_node_scope_only(switchboard, capsys):
    assert cli.main(["set", "widgets", "on", "--scope", "account", "--id", "acct"]) == 1
    err = capsys.readouterr().err
    assert "node scope only" in err and "never for an account or a user" in err
    assert cli.main(["set", "chat", "on", "--scope", "user", "--id", "u1"]) == 1
    assert cli.main(["set", "widgets", "soon"]) == 1
    assert "soon" in capsys.readouterr().err
    assert switchboard["store"].rows() == []  # nothing was stored for the refused writes

    assert cli.main(["set", "widgets", "on"]) == 0
    assert _rows(capsys, "list", "--kind", "extension")["rows"][0]["enabled"] is True


def test_a_name_that_is_both_a_flag_and_a_command_is_each_nouns_own(switchboard, capsys):
    from axiom.extensions.builtins.features import flag_registry
    from axiom.extensions.builtins.features.flag_registry import FlagDef
    from axiom.extensions.builtins.features.tests.conftest import FLAGS

    flag_registry.set_registry((*FLAGS, FlagDef(key="chat", description="the chat verb", default="off", owner="chat")))
    # `flags set` sets the flag; `features set` the command, with or without `--kind`.
    assert flags_cli.main(["set", "chat", "on", "--scope", "user", "--id", "u1"]) == 0
    assert [r["key"] for r in switchboard["store"].rows("chat")] == ["chat"]
    assert cli.main(["set", "chat", "on"]) == 0
    from axiom.infra import node_functions as nf

    assert "chat" in nf.load().features
    # `enable` prefers the node switch, which is what it always meant.
    assert cli.main(["disable", "chat"]) == 0
    assert "chat" not in nf.load().features


def test_an_unknown_name_is_refused_and_points_at_the_list(switchboard, capsys):
    assert cli.main(["enable", "nothing_here"]) == 1
    assert "features list" in capsys.readouterr().err
