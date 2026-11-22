# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F8: for a flag and a subject, the value, what decided it, the rule and cohorts matched
(nested), the bucket, every override ignored and why, and every unknown attribute."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.flags import cli
from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.tests.conftest import mcp, svc, web


def _world(loop):
    ts, s = loop["targeting"], svc()
    ts.put_cohort("operators", rules={"attr": "role", "op": "contains", "value": "site-admin"}, by="@t")
    ts.put_cohort("early", rules={"and": [{"in_cohort": "operators"},
                                          {"attr": "account_kind", "op": "neq", "value": "trial"}]},
                  principals=["ana"], by="@t")
    ts.put_rollout("beta_editor", "development", rules=[
        {"cohort": "early", "value": "on", "percentage": 100},
        {"value": "on", "percentage": 100 - 1e-9},
    ], reason="r", by="@t")
    # An expired user override: present, ignored, reported.
    s.overrides.put("user", "ana", "beta_editor", "off", by="@t", reason="old test",
                    expires_at=datetime.now(UTC) - timedelta(days=1))
    return s


@pytest.mark.acceptance("LOOP-F8")
def test_explain_carries_value_decision_rule_cohorts_bucket_ignored_and_unknown(loop):
    s = _world(loop)
    out = s.explain("beta_editor", user="ana", account="acme")
    assert out["value"] == "on" and out["decided_by"] == "rollout"
    assert out["detail"]["rule_index"] == 0 and out["detail"]["cohort"] == "early"
    assert out["detail"]["cohorts_matched"] == {"early": True}  # listed: no rule read
    assert out["ignored"] == [{"step": "user", "status": "expired", "value": "off", "reason": "old test",
                               "expires_at": out["ignored"][0]["expires_at"], "set_by": "@t"}]
    bob = s.explain("beta_editor", user="bob", roles=["site-admin"])
    # Nested cohorts are reported: early read operators; account_kind was unknown.
    assert bob["evaluated"][0]["cohorts_matched"] == {"early": None, "operators": True}
    assert bob["unknown_attributes"] == ["account_kind"]
    assert bob["decided_by"] == "rollout" and bob["detail"]["rule_index"] == 1
    assert bob["bucket"] == t.bucket("beta_editor", "bob") and bob["bucket_by"] == "principal"
    for key in ("flag", "environment", "subject", "protected", "kill_switch", "rules", "cohorts"):
        assert key in out


@pytest.mark.acceptance("LOOP-F8")
def test_explain_at_the_command_line(loop, capsys):
    _world(loop)
    assert cli.main(["explain", "beta_editor", "--user", "bob", "--role", "site-admin"]) == 0
    text = capsys.readouterr().out
    assert "decided by: rollout" in text and "rule 1" in text and "account_kind" in text
    assert cli.main(["--json", "explain", "beta_editor", "--user", "ana"]) == 0
    assert json.loads(capsys.readouterr().out)["decided_by"] == "rollout"


@pytest.mark.acceptance("LOOP-F8")
def test_explain_over_mcp(loop):
    _world(loop)
    out = mcp("axiom_flags__explain", {"key": "beta_editor", "user": "ana"}, loop["state"])
    assert out["ok"] and out["value"]["decided_by"] == "rollout"
    assert out["value"]["ignored"][0]["status"] == "expired"


@pytest.mark.acceptance("LOOP-F8")
def test_explain_over_http_for_the_signed_in_caller(loop, monkeypatch):
    _world(loop)
    client = web(monkeypatch, sub="bob", roles=("site-admin",), site="acme")
    body = client.get("/api/v1/features/beta_editor/explain").json()
    assert body["subject"]["principal"] == "bob" and body["decided_by"] == "rollout"
    # The session's site-admin role is read; the unknown account kind keeps bob out of `early`.
    assert body["evaluated"][0]["cohorts_matched"]["operators"] is True
    assert body["detail"]["rule_index"] == 1 and body["unknown_attributes"] == ["account_kind"]
    # Somebody else's answer is for operators only.
    assert client.get("/api/v1/features/beta_editor/explain?user=ana").json()["subject"]["principal"] == "ana"
    viewer = web(monkeypatch, sub="cy", roles=("viewer",), site="acme")
    assert viewer.get("/api/v1/features/beta_editor/explain?user=ana").status_code == 403
    assert viewer.get("/api/v1/features/nope/explain").status_code == 404


@pytest.mark.acceptance("LOOP-F8")
def test_explain_names_an_undeclared_flag(loop):
    out = mcp("axiom_flags__explain", {"key": "nope"}, loop["state"])
    assert not out["ok"] and "no flag 'nope'" in out["errors"][0]
