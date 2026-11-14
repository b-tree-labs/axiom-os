# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F12 (one evaluation, on the server, on every surface) and LOOP-F13 (cohort rules live
only in the record; a node with no database resolves without them and says so)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.features import resolver
from axiom.extensions.builtins.flags import cli
from axiom.extensions.builtins.features.tests.conftest import mcp, svc, web

MIGRATION = Path(resolver.__file__).parents[1] / "flags" / "migrations" / "versions"


def _rollout(loop):
    ts = loop["targeting"]
    ts.put_cohort("operators", rules={"attr": "role", "op": "contains", "value": "site-admin"}, by="@t")
    ts.put_rollout("beta_editor", "development", rules=[{"cohort": "operators", "value": "on"},
                                                        {"value": "soon", "percentage": 50}],
                   reason="r", by="@t")


@pytest.mark.acceptance("LOOP-F12")
def test_every_surface_gets_the_same_answer_from_one_evaluation(loop, monkeypatch, capsys):
    _rollout(loop)
    for user in ("u1", "u2", "u3", "u4", "u5", "u6"):
        expected = svc().resolve(user=user, account="acme")
        want = (expected.flags["beta_editor"], expected.sources["beta_editor"])
        assert cli.main(["--json", "resolve", "beta_editor", "--user", user, "--account", "acme"]) == 0
        got = json.loads(capsys.readouterr().out)
        assert (got["flags"]["beta_editor"], got["sources"]["beta_editor"]) == want
        got = mcp("axiom_flags__resolve", {"key": "beta_editor", "user": user, "account": "acme"},
                  loop["state"])["value"]
        assert (got["flags"]["beta_editor"], got["sources"]["beta_editor"]) == want
        body = web(monkeypatch, sub=user, site="acme").get("/api/v1/features").json()
        assert (body["flags"]["beta_editor"], body["sources"]["beta_editor"]) == want
        assert body["flags"] == web(monkeypatch, sub=user, site="acme").get("/api/v1/flags").json()["flags"]


@pytest.mark.acceptance("LOOP-F12")
def test_the_web_slice_resolves_the_session_role_on_the_server(loop, monkeypatch):
    _rollout(loop)
    body = web(monkeypatch, sub="u1", roles=("site-admin",), site="acme").get("/api/v1/features").json()
    assert body["flags"]["beta_editor"] == "on" and body["sources"]["beta_editor"] == "rollout"
    # The client receives resolved values only: no rules, no cohorts.
    assert "rollouts" not in body and "cohorts" not in json.dumps(body["metadata"])


@pytest.mark.acceptance("LOOP-F13")
def test_cohort_and_rollout_writes_never_touch_the_node_file(loop):
    node = loop["node"]
    node.write_text('[node]\nfeatures = []\n')
    before = node.read_bytes()
    assert cli.main(["cohort", "set", "pilots", "--principal", "ana", "--reason", "r", "--apply"]) == 0
    assert cli.main(["rollout", "set", "beta_editor", "--rules", '[{"cohort": "pilots", "value": "on"}]',
                     "--reason", "r", "--apply"]) == 0
    assert cli.main(["kill", "beta_editor", "--reason", "r"]) == 0
    assert node.read_bytes() == before


@pytest.mark.acceptance("LOOP-F13")
def test_a_node_with_no_database_resolves_without_cohorts_and_says_so(loop, capsys):
    s = resolver.FlagService(overrides=resolver.NoOverrides())
    res = s.resolve(user="ana")
    assert res.flags["beta_editor"] == "off" and res.sources["beta_editor"] == "default"
    assert "no database" in res.targeting
    out = s.explain("beta_editor", user="ana")
    assert out["cohorts"]["available"] is False and "no database" in out["cohorts"]["why"]
    loop["node"].write_text('[node]\nrole = "daq"\nfunctions = ["acquire", "transmit"]\nfeatures = []\n')
    # The resolve verb on such a node says so too.
    assert cli.main(["--json", "resolve", "beta_editor", "--user", "ana"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["targeting"]


@pytest.mark.acceptance("LOOP-F13")
def test_an_unreadable_record_resolves_without_cohorts_and_says_so(loop):
    class Broken:
        def rollouts(self, env):
            raise RuntimeError("relation flags.rollouts does not exist")

        def cohorts(self):
            raise RuntimeError("unreachable")

    res = resolver.FlagService(targeting=Broken()).resolve(user="ana")
    assert res.sources["beta_editor"] == "default"
    assert "could not be read" in res.targeting


@pytest.mark.acceptance("LOOP-F13")
def test_the_migration_only_adds(loop):
    from axiom.infra.deploy.migration_guard import contracting_operations

    path = MIGRATION / "0002_cohorts_rollouts_and_reasoned_overrides.py"
    assert path.exists()
    assert contracting_operations(path.read_text().split("def downgrade")[0]) == []
