# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Q14 (owner decision): one level of nesting everywhere.

A cohort or rollout rule is a group (all/any, also spelled and/or) that may hold groups of its
own, and no deeper: put an inner group in its own cohort and say "is in cohort". ``not`` adds no
level around a single test or a cohort membership. Membership by reference stays unlimited, and
a cycle is still refused. Every surface refuses a deeper rule with one plain message, and a
read-only audit lists stored rules that are deeper; nothing is rewritten.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.tests.conftest import mcp, run, web

LEAF = {"attr": "locale", "op": "eq", "value": "en-US"}
MESSAGE = ("This rule nests groups more than one level deep. Put the inner group in its own cohort and use "
           "'is in cohort' instead.")
ALLOWED = (
    LEAF,
    {"not": LEAF},
    {"not": {"in_cohort": "staff"}},
    {"all": [LEAF, {"any": [LEAF, {"not": LEAF}]}]},
    {"and": [{"not": {"in_cohort": "staff"}}, {"or": [LEAF, LEAF]}]},
    {"not": {"and": [LEAF, LEAF]}},
)
TOO_DEEP = (
    {"all": [{"any": [{"all": [LEAF]}]}]},
    {"and": [{"not": {"or": [LEAF, LEAF]}}]},
    {"not": {"and": [{"or": [LEAF]}]}},
)


@pytest.mark.acceptance("Q14")
def test_q14_one_group_inside_another_and_no_deeper(modes):
    """Q14: check_cohort and check_rollout accept a group of groups and refuse a deeper one in words."""
    ts = modes["targeting"]
    ts.put_cohort("staff", principals=["ana"], by="@t")
    for rule in ALLOWED:
        ts.check_cohort("ok", rules=rule)
        ts.check_rollout([{"match": rule, "value": "on"}])
    for rule in TOO_DEEP:
        with pytest.raises(t.RuleError) as cohort_err:
            ts.check_cohort("deep", rules=rule)
        assert MESSAGE in str(cohort_err.value)
        with pytest.raises(t.RuleError) as rollout_err:
            ts.check_rollout([{"match": rule, "value": "on"}])
        assert MESSAGE in str(rollout_err.value)


@pytest.mark.acceptance("Q14")
def test_q14_every_surface_says_the_same(modes, monkeypatch):
    """Q14: the draft tools, MCP, the web and the command line refuse a deeper rule with the one message."""
    from axiom.extensions.builtins.flags import cli

    deep = TOO_DEEP[0]
    out = run("flags.cohort_draft_create", {"key": "deep", "body": {"rules": deep}}, modes["state"], actor="@ana:acct")
    assert not out.ok and MESSAGE in out.errors[0]
    out = mcp("axiom_flags__cohort_preview", {"key": "deep", "rules": deep}, modes["state"])
    assert not out["ok"] and MESSAGE in out["errors"][0]
    r = web(monkeypatch, sub="ana", roles=("site-admin",), site="acct").put(
        "/api/v1/flags/drafts/cohorts/deep", json={"body": {"rules": deep}})
    assert r.status_code == 400 and MESSAGE in r.json()["detail"]
    import json

    assert cli.main(["cohort", "set", "deep", "--rule", json.dumps(deep)]) != 0


@pytest.mark.acceptance("Q14")
def test_q14_membership_by_reference_stays_unlimited_and_cycles_are_refused(modes):
    """Q14: a chain of cohorts that each read the next is not a nesting limit; a cycle still is refused."""
    ts = modes["targeting"]
    ts.put_cohort("c0", principals=["ana"], by="@t")
    for i in range(1, 5):
        ts.put_cohort(f"c{i}", rules={"in_cohort": f"c{i - 1}"}, by="@t")
    ts.check_cohort("c5", rules={"all": [{"in_cohort": "c4"}, {"any": [LEAF, {"in_cohort": "c3"}]}]})
    with pytest.raises(t.RuleError, match="cycle"):
        ts.put_cohort("c0", rules={"in_cohort": "c4"}, by="@t")


@pytest.mark.acceptance("Q14")
def test_q14_the_audit_lists_stored_rules_that_are_too_deep_and_changes_nothing(modes):
    """Q14: ``flags.rule_depth_audit`` reads every stored cohort and rollout rule and names the ones
    deeper than one level, with the message; it writes nothing."""
    ts = modes["targeting"]
    ts.put_cohort("fine", rules=ALLOWED[3], by="@t", validate=False)
    ts.put_cohort("deep", rules=TOO_DEEP[0], by="@t", validate=False)
    ts.put_rollout("beta_editor", "development", rules=[{"match": TOO_DEEP[1], "value": "on"}], reason="r", by="@t",
                   validate=False)
    out = run("flags.rule_depth_audit", {}, modes["state"])
    assert out.ok, out.errors
    found = {(r["kind"], r["key"], r.get("environment")) for r in out.value["too_deep"]}
    assert found == {("cohort", "deep", None), ("rollout", "beta_editor", "development")}
    assert all(r["why"] == MESSAGE for r in out.value["too_deep"])
    assert out.value["checked"] == {"cohorts": 2, "rollouts": 1}
    assert ts.cohort("deep")["rules"] == TOO_DEEP[0]  # nothing is rewritten


@pytest.mark.acceptance("Q14")
def test_q14_the_mcp_schema_says_one_level(modes):
    """Q14: the draft tools' rule schema says one level of groups and points at 'is in cohort'."""
    from axiom.extensions.builtins.features.skills import bind
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    tool = next(x for x in skill_tool_contribution(reg, ctx_factory=lambda: None).tools
                if x.name == "axiom_flags__cohort_draft_update")
    text = tool.input_schema["$defs"]["rule"]["description"]
    assert "one level" in text and "in_cohort" in text
