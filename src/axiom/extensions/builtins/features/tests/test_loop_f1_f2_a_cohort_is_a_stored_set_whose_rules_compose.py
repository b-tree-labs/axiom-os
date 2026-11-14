# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F1 (a cohort is a named, stored set) and LOOP-F2 (rules compose) (ADR-257 D5)."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.tests.conftest import svc


def _attrs(**values):
    def lookup(name):
        if name in values:
            return t.Attr(available=True, value=values[name])
        return t.Attr(available=False)

    return lookup


@pytest.mark.acceptance("LOOP-F1")
def test_a_member_is_listed_or_matches_the_rule(loop):
    ts = loop["targeting"]
    ts.put_cohort("pilots", name="Pilots", principals=["ana"], accounts=["acme"],
                  rules={"attr": "role", "op": "contains", "value": "site-admin"}, by="@t")
    ts.put_rollout("beta_editor", "development", rules=[{"cohort": "pilots", "value": "on"}],
                   reason="pilot", by="@t")
    s = svc()
    assert s.resolve_flag("beta_editor", user="ana") == "on"            # listed principal
    assert s.resolve_flag("beta_editor", user="zed", account="acme") == "on"  # listed account
    assert s.resolve_flag("beta_editor", user="zed", roles=["site-admin"]) == "on"  # the rule
    assert s.resolve_flag("beta_editor", user="zed", roles=["viewer"]) == "off"
    stored = ts.cohort("pilots")
    assert stored["principals"] == ["ana"] and stored["accounts"] == ["acme"]
    assert stored["name"] == "Pilots" and stored["updated_by"] == "@t"


@pytest.mark.acceptance("LOOP-F1")
def test_a_cohort_with_only_a_list_never_reads_an_attribute(loop):
    ts = loop["targeting"]
    ts.put_cohort("listed", principals=["ana"], by="@t")
    ts.put_rollout("beta_editor", "development", rules=[{"cohort": "listed", "value": "on"}],
                   reason="r", by="@t")
    out = svc().explain("beta_editor", user="bob")
    assert out["value"] == "off" and out["unknown_attributes"] == []


@pytest.mark.acceptance("LOOP-F2")
@pytest.mark.parametrize(
    ("op", "want", "have", "expected"),
    [
        ("eq", "Lab", "lab", True),  # strings ignore case
        ("neq", "lab", "field", True),
        ("in", ["a", "b"], "b", True),
        ("not_in", ["a", "b"], "c", True),
        ("contains", "site-admin", ["viewer", "site-admin"], True),
        ("gt", 3, 4, True),
        ("gte", 4, 4, True),
        ("lt", 3, 4, False),
        ("lte", 4, 4, True),
        ("exists", None, "x", True),
    ],
)
def test_every_leaf_operator(op, want, have, expected):
    rule = {"attr": "a", "op": op} if op == "exists" else {"attr": "a", "op": op, "value": want}
    t.validate_rule(rule)
    assert t.evaluate(rule, _attrs(a=have)).value is expected


@pytest.mark.acceptance("LOOP-F2")
def test_and_or_not_and_in_cohort_compose():
    rule = {"and": [
        {"or": [{"attr": "a", "op": "eq", "value": 1}, {"attr": "b", "op": "eq", "value": 2}]},
        {"not": {"in_cohort": "blocked"}},
    ]}
    t.validate_rule(rule, known_cohorts={"blocked"})
    member = {"blocked": False}
    assert t.evaluate(rule, _attrs(a=0, b=2), member.get).value is True
    member["blocked"] = True
    assert t.evaluate(rule, _attrs(a=0, b=2), member.get).value is False


@pytest.mark.acceptance("LOOP-F2")
def test_depth_and_size_are_limited_at_save_time(loop):
    deep = {"attr": "role", "op": "exists"}
    for _ in range(t.MAX_RULE_DEPTH + 1):
        deep = {"not": deep}
    with pytest.raises(t.RuleError, match="deeper"):
        loop["targeting"].put_cohort("deep", rules=deep, by="@t", validate=True)
    wide = {"or": [{"attr": "role", "op": "exists"}] * (t.MAX_RULE_NODES + 1)}
    with pytest.raises(t.RuleError, match="nodes"):
        loop["targeting"].put_cohort("wide", rules=wide, by="@t", validate=True)
    assert loop["targeting"].cohort("deep") is None and loop["targeting"].cohort("wide") is None


@pytest.mark.acceptance("LOOP-F2")
def test_a_cycle_is_refused_with_its_path_named(loop):
    ts = loop["targeting"]
    ts.put_cohort("a", rules={"attr": "role", "op": "exists"}, by="@t", validate=True)
    ts.put_cohort("b", rules={"in_cohort": "a"}, by="@t", validate=True)
    with pytest.raises(t.RuleError, match=r"a -> b -> a"):
        ts.put_cohort("a", rules={"in_cohort": "b"}, by="@t", validate=True)
    assert ts.cohort("a")["rules"] == {"attr": "role", "op": "exists"}  # unchanged


@pytest.mark.acceptance("LOOP-F2")
def test_an_unknown_cohort_operator_or_attribute_is_refused(loop):
    ts = loop["targeting"]
    with pytest.raises(t.RuleError, match="Unknown cohort 'ghost'"):
        ts.put_cohort("x", rules={"in_cohort": "ghost"}, by="@t", validate=True)
    with pytest.raises(t.RuleError, match="Unknown operator"):
        ts.put_cohort("x", rules={"attr": "role", "op": "like", "value": "a"}, by="@t", validate=True)
    with pytest.raises(t.RuleError, match="Unknown attribute 'shoe_size'"):
        ts.put_cohort("x", rules={"attr": "shoe_size", "op": "exists"}, by="@t", validate=True)
