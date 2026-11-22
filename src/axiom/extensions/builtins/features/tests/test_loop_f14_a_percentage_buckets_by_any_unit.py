# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F14: a percentage rollout chooses its unit (any catalogue attribute, or a cohort);
everyone who shares the unit's value is in or out together, and a subject whose unit is
UNKNOWN is never admitted by a percentage."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import attributes
from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.attributes import AttributeDef
from axiom.extensions.builtins.features.tests.conftest import svc


@pytest.mark.acceptance("LOOP-F14")
def test_every_subject_on_one_node_moves_together(modes):
    modes["targeting"].put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 50}],
                                   bucket_by="node", reason="half the nodes", by="@t")
    s = svc()
    nodes = [f"n-{i}" for i in range(40)]
    for n in nodes:
        values = {s.resolve_flag("beta_editor", node=n, user=u) for u in ("ana", "bob", "cy", "dee")}
        assert len(values) == 1, f"principals on {n} split: {values}"
    on = {n for n in nodes if s.resolve_flag("beta_editor", node=n, user="ana") == "on"}
    assert on == {n for n in nodes if t.bucket("beta_editor", "node=" + n) < 50}
    assert 8 < len(on) < 32
    out = s.explain("beta_editor", node="n-3", user="ana")
    assert out["bucket_by"] == "node" and out["unit_value"] == "n-3"


@pytest.mark.acceptance("LOOP-F14")
def test_an_unknown_unit_is_never_admitted_by_a_percentage(modes):
    modes["targeting"].put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 99.99}],
                                   bucket_by="country", reason="r", by="@t")
    out = svc().explain("beta_editor", user="ana")  # no country known for ana
    assert out["value"] == "off"
    assert {"step": "rollout", "status": "unit_unknown", "rule_index": 0, "unit": "country"} in out["ignored"]
    assert svc().resolve_flag("beta_editor", user="ana", country="NZ") in ("on", "off")


@pytest.mark.acceptance("LOOP-F14")
def test_a_cohort_unit_moves_each_listed_cohort_as_one(modes):
    ts = modes["targeting"]
    rings = {f"ring-{i}": [f"r{i}-{j}" for j in range(5)] for i in range(12)}
    for key, members in rings.items():
        ts.put_cohort(key, principals=members, by="@t")
    unit = "cohort:" + ",".join(rings)
    ts.put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 50}],
                   bucket_by=unit, reason="half the rings", by="@t")
    s = svc()
    for key, members in rings.items():
        values = {s.resolve_flag("beta_editor", user=m) for m in members}
        assert values == {"on" if t.bucket("beta_editor", "cohort=" + key) < 50 else "off"}
    out = s.explain("beta_editor", user="outsider")
    assert out["value"] == "off" and any(i["status"] == "unit_unknown" for i in out["ignored"])


@pytest.mark.acceptance("LOOP-F14")
def test_a_rule_may_bucket_by_an_extension_declared_attribute(modes):
    teams = {"ana": "red", "bob": "red", "cy": "blue", "dee": "blue", "eve": "green"}
    attributes.set_extension_attributes(
        [AttributeDef("team", "the caller's team", lambda s: teams.get(s.principal or ""), owner="ext")]
    )
    modes["targeting"].put_rollout("beta_editor", "development", rules=[
        {"value": "on", "percentage": 50, "bucket_by": "team"}], reason="r", by="@t")
    s = svc()
    assert s.resolve_flag("beta_editor", user="ana") == s.resolve_flag("beta_editor", user="bob")
    assert s.resolve_flag("beta_editor", user="cy") == s.resolve_flag("beta_editor", user="dee")


@pytest.mark.acceptance("LOOP-F14")
def test_a_unit_that_is_not_in_the_catalogue_is_refused(modes):
    ts = modes["targeting"]
    with pytest.raises(t.RuleError, match="unit"):
        ts.put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 5}],
                       bucket_by="shoe_size", reason="r", by="@t")
    with pytest.raises(t.RuleError, match="cohort"):
        ts.put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 5}],
                       bucket_by="cohort:ghost", reason="r", by="@t")
    assert ts.rollout("beta_editor", "development") is None


@pytest.mark.acceptance("LOOP-F14")
def test_principal_and_account_buckets_are_unchanged(modes):
    """A rollout bucketed before this change keeps its assignment."""
    ts = modes["targeting"]
    ts.put_rollout("by_account", "development", rules=[{"value": "on", "percentage": 50}], reason="r", by="@t")
    out = svc().explain("by_account", user="ana", account="acme")
    assert out["bucket"] == t.bucket("by_account", "acme")
    ts.put_rollout("by_account", "development", rules=[{"value": "on", "percentage": 50}],
                   bucket_by="principal", reason="r", by="@t")
    assert ts.rollout("by_account", "development")["bucket_by"] == "principal"
    assert svc().explain("by_account", user="ana")["bucket"] == t.bucket("by_account", "ana")
