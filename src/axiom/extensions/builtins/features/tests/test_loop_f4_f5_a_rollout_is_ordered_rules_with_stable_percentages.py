# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F4 (a rollout is an ordered list of rules per flag and environment) and LOOP-F5
(percentage assignment is stable and independent between flags)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.tests.conftest import svc


@pytest.mark.acceptance("LOOP-F4")
def test_the_first_rule_that_decides_wins(loop):
    ts = loop["targeting"]
    ts.put_cohort("staff", principals=["ana"], by="@t")
    ts.put_rollout("beta_editor", "development", rules=[
        {"cohort": "staff", "value": "off", "note": "staff see the old editor"},
        {"match": {"attr": "role", "op": "contains", "value": "site-admin"}, "value": "soon"},
        {"value": "on"},  # no target: everyone left
    ], reason="r", by="@t")
    s = svc()
    assert s.resolve_flag("beta_editor", user="ana", roles=["site-admin"]) == "off"
    assert s.resolve_flag("beta_editor", user="bob", roles=["site-admin"]) == "soon"
    assert s.resolve_flag("beta_editor", user="bob", roles=["viewer"]) == "on"
    out = s.explain("beta_editor", user="bob", roles=["viewer"])
    assert out["decided_by"] == "rollout" and out["detail"]["rule_index"] == 2


@pytest.mark.acceptance("LOOP-F4")
def test_a_rollout_is_per_environment(loop, monkeypatch):
    loop["targeting"].put_rollout("beta_editor", "staging", rules=[{"value": "on"}], reason="r", by="@t")
    assert svc().resolve_flag("beta_editor", user="ana") == "off"
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "staging")
    assert svc().resolve_flag("beta_editor", user="ana") == "on"


@pytest.mark.acceptance("LOOP-F4")
def test_a_rule_applies_only_inside_its_time_window(loop):
    now = datetime.now(UTC)
    loop["targeting"].put_rollout("beta_editor", "development", rules=[
        {"value": "on", "active_from": (now + timedelta(days=1)).isoformat()},
        {"value": "soon", "active_until": (now + timedelta(days=1)).isoformat()},
    ], reason="r", by="@t")
    assert svc().resolve_flag("beta_editor", user="ana") == "soon"


@pytest.mark.acceptance("LOOP-F4")
def test_a_malformed_rollout_is_refused(loop):
    ts = loop["targeting"]
    with pytest.raises(t.RuleError, match="unknown cohort"):
        ts.put_rollout("beta_editor", "development", rules=[{"cohort": "ghost", "value": "on"}], reason="r", by="@t")
    with pytest.raises(t.RuleError, match="not both"):
        ts.put_rollout("beta_editor", "development", rules=[
            {"cohort": "x", "match": {"attr": "role", "op": "exists"}, "value": "on"}], reason="r", by="@t")
    with pytest.raises(t.RuleError, match="percentage"):
        ts.put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 120}], reason="r", by="@t")
    with pytest.raises(t.RuleError, match="on, off or soon"):
        ts.put_rollout("beta_editor", "development", rules=[{"value": "maybe"}], reason="r", by="@t")
    assert ts.rollout("beta_editor", "development") is None


@pytest.mark.acceptance("LOOP-F5")
def test_a_bucket_is_stable_and_independent_between_flags():
    assert t.bucket("beta_editor", "ana") == t.bucket("beta_editor", "ana")
    assert 0 <= t.bucket("beta_editor", "ana") < 100
    subjects = [f"user-{i}" for i in range(400)]
    first = {s for s in subjects if t.bucket("beta_editor", s) < 20}
    second = {s for s in subjects if t.bucket("fast_search", s) < 20}
    assert 50 < len(first) < 110  # roughly a fifth
    assert len(first & second) < len(first) / 2  # not the same people first in every rollout


@pytest.mark.acceptance("LOOP-F5")
def test_a_percentage_admits_the_same_subjects_across_requests(loop):
    loop["targeting"].put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 30}],
                                  reason="r", by="@t")
    subjects = [f"user-{i}" for i in range(60)]
    once = {s: svc().resolve_flag("beta_editor", user=s) for s in subjects}
    again = {s: svc().resolve_flag("beta_editor", user=s) for s in subjects}
    assert once == again
    assert {s for s, v in once.items() if v == "on"} == {s for s in subjects if t.bucket("beta_editor", s) < 30}


@pytest.mark.acceptance("LOOP-F5")
def test_the_bucket_subject_is_the_principal_by_default_and_the_account_when_declared(loop):
    ts = loop["targeting"]
    ts.put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 50}], reason="r", by="@t")
    ts.put_rollout("by_account", "development", rules=[{"value": "on", "percentage": 50}], reason="r", by="@t")
    s = svc()
    out = s.explain("beta_editor", user="ana", account="acme")
    assert out["bucket_by"] == "principal" and out["bucket"] == t.bucket("beta_editor", "ana")
    out = s.explain("by_account", user="ana", account="acme")
    assert out["bucket_by"] == "account" and out["bucket"] == t.bucket("by_account", "acme")
    # Every principal of one account shares the account's assignment.
    values = {s.resolve_flag("by_account", user=u, account="acme") for u in ("a", "b", "c", "d")}
    assert len(values) == 1
    # A rollout row may set the subject for its environment.
    ts.put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 50}],
                   bucket_by="account", reason="r", by="@t")
    assert s.explain("beta_editor", user="ana", account="acme")["bucket_by"] == "account"


@pytest.mark.acceptance("LOOP-F5")
def test_no_subject_to_bucket_means_the_percentage_cannot_admit(loop):
    loop["targeting"].put_rollout("beta_editor", "development", rules=[{"value": "on", "percentage": 99.99}],
                                  reason="r", by="@t")
    out = svc().explain("beta_editor")
    assert out["value"] == "off"
    assert {"step": "rollout", "status": "no_subject_to_bucket", "rule_index": 0} in out["ignored"]
