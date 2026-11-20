# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One audit for every switch write (ADR-254 §4), and the record beside the node file (§6)."""

from __future__ import annotations

from axiom.extensions.builtins.features import cli
from axiom.extensions.builtins.features.tests.conftest import mcp, web
from axiom.extensions.builtins.flags import cli as flags_cli
from axiom.infra import audit_trail


def _records(state) -> list[dict]:
    return [r for r in audit_trail.read_chain(audit_trail.audit_path(state))
            if r["skill"].startswith(("features.", "flags."))]


def test_enable_disable_and_policy_writes_land_in_the_audit_chain_with_their_change(switchboard, capsys):
    state = switchboard["state"]
    # A role node, so a command is off until turned on; the role's function holds the database.
    switchboard["node"].write_text('[node]\nrole = "assistant-only"\nfunctions = ["assistant"]\nfeatures = []\n')
    assert cli.main(["enable", "widget"]) == 1  # its extension is off: refused, with the pointer
    assert "features enable widgets" in capsys.readouterr().err
    assert cli.main(["enable", "widgets"]) == 0
    assert cli.main(["enable", "widget"]) == 0
    assert cli.main(["agents", "local"]) == 0
    assert flags_cli.main(["set", "timeline", "on"]) == 0
    assert cli.main(["disable", "widget"]) == 0

    records = _records(state)
    assert [(r["skill"], r["outcome"]) for r in records] == [
        ("features.enable", "error"),
        ("features.enable", "ok"),
        ("features.enable", "ok"),
        ("features.agents", "ok"),
        ("flags.set", "ok"),
        ("features.disable", "ok"),
    ]
    assert "change" not in records[0]
    assert records[1]["change"] == {"kind": "extension", "subject": "widgets", "old": "off", "new": "on"}
    assert records[2]["change"] == {"kind": "function", "subject": "widget", "old": "off", "new": "on"}
    assert records[3]["change"] == {"kind": "function", "subject": "agents", "old": "none", "new": "local"}
    assert records[4]["change"]["kind"] == "flag" and records[4]["change"]["new"] == "on"
    assert records[5]["change"] == {"kind": "function", "subject": "widget", "old": "on", "new": "off"}
    assert {r["principal"] for r in records} == {audit_trail.DEFAULT_PRINCIPAL}
    assert audit_trail.verify_chain(audit_trail.audit_path(state))


def test_the_change_log_shows_switch_writes_by_kind(switchboard):
    from axiom.extensions.builtins.settings.changes import KINDS, read_changes

    assert {"extension", "function", "flag"} <= set(KINDS)
    switchboard["node"].write_text('[node]\nrole = "assistant-only"\nfunctions = ["assistant"]\nfeatures = []\n')
    assert cli.main(["enable", "widgets"]) == 0
    assert cli.main(["enable", "widget"]) == 0
    rows = read_changes(switchboard["state"], kind="extension")
    assert [(r["what"], r["old"], r["new"]) for r in rows] == [("widgets", "off", "on")]
    rows = read_changes(switchboard["state"], kind="function")
    assert [(r["what"], r["new"]) for r in rows] == [("widget", "on")]


def test_the_web_and_mcp_writes_carry_the_caller_as_principal(switchboard, monkeypatch):
    state = switchboard["state"]
    r = web(monkeypatch, roles=["site-admin"]).patch("/api/v1/features/widgets", json={"value": "on"})
    assert r.status_code == 200, r.text
    assert r.json()["set"]["kind"] == "extension" and r.json()["set"]["updated_by"] == "@u9:acct"
    out = mcp("axiom_flags__set", {"key": "timeline", "value": "on", "scope": "user", "scope_id": "u9"}, state)
    assert out["ok"], out
    records = _records(state)
    assert records[0]["principal"] == "@u9:acct" and records[0]["change"]["kind"] == "extension"
    assert records[1]["change"]["kind"] == "flag"


def test_a_viewer_cannot_flip_a_node_switch_over_the_web(switchboard, monkeypatch):
    r = web(monkeypatch, roles=["viewer"]).patch("/api/v1/features/widgets", json={"value": "on"})
    assert r.status_code == 403
    r = web(monkeypatch, roles=["viewer"]).patch("/api/v1/features/widgets", json={"value": "on", "scope": "account"})
    assert r.status_code == 403
    from axiom.infra import node_functions as nf

    assert "widgets" not in nf.load().features


def test_a_node_scope_switch_is_recorded_beside_the_node_file_with_who_set_it(switchboard, capsys):
    store = switchboard["store"]
    assert cli.main(["enable", "widgets"]) == 0
    assert cli.main(["enable", "widget"]) == 0
    rows = {r["key"]: r for r in store.rows()}
    assert rows["extension:widgets"]["value"] == "on" and rows["extension:widgets"]["updated_by"] == "@cli:local"
    assert rows["function:widget"]["value"] == "on" and rows["function:widget"]["scope_id"] == "node-a"
    assert cli.main(["disable", "widgets"]) == 0
    assert {r["key"]: r["value"] for r in store.rows()}["extension:widgets"] == "off"
    assert cli.main(["set", "widgets", "clear"]) == 0
    assert "extension:widgets" not in {r["key"] for r in store.rows()}
    # The recorded rows never leak into the flag listing.
    assert flags_cli.main(["list"]) == 0
    assert "function:" not in capsys.readouterr().out


def test_drift_between_the_record_and_the_node_file_is_said_not_fixed(switchboard, capsys):
    import json

    assert cli.main(["enable", "widgets"]) == 0
    switchboard["node"].write_text('[node]\nfunctions = []\nfeatures = []\n')  # hand-edited back off
    capsys.readouterr()
    assert cli.main(["--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["drift"] and "widgets" in value["drift"][0] and "features enable widgets" in value["drift"][0]
    rows = {r["name"]: r for r in value["rows"]}
    assert rows["widgets"]["enabled"] is False  # the node file is what runs
