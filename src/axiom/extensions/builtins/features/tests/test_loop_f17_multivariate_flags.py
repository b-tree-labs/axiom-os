# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F17: a flag may serve a string, a number or a document from a declared set of
variants; a rule serves one variant or splits a unit across variants by weight, with
stable bucketing; on/off/soon flags keep working unchanged."""

from __future__ import annotations

from collections import Counter

import pytest

from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.flag_registry import FlagDef
from axiom.extensions.builtins.features.tests.conftest import svc, web


@pytest.mark.acceptance("LOOP-F17")
def test_a_flag_declares_variants_of_any_json_type_and_a_default(modes):
    flag = svc().get_registry()["layout"]
    assert flag.multivariate and set(flag.variants) == {"compact", "wide", "ratio"}
    assert flag.default == "compact"
    res = svc().resolve(user="ana")
    assert res.flags["layout"] == "compact" and res.values["layout"] == "compact"
    assert "beta_editor" not in res.values  # an on/off/soon flag has no variant value
    with pytest.raises(ValueError, match="variant"):
        FlagDef.from_mapping({"key": "bad", "variants": {"a": 1}, "default": "b"})
    with pytest.raises(ValueError, match="variant"):
        FlagDef.from_mapping({"key": "bad", "variants": {"Not A Name": 1}, "default": "Not A Name"})


@pytest.mark.acceptance("LOOP-F17")
def test_a_rule_serves_one_variant_with_its_value(modes):
    ts = modes["targeting"]
    ts.put_cohort("staff", principals=["ana"], by="@t")
    ts.put_rollout("layout", "development", rules=[{"cohort": "staff", "value": "wide"}], reason="r", by="@t")
    s = svc()
    res = s.resolve(user="ana")
    assert res.flags["layout"] == "wide" and res.values["layout"] == {"columns": 3}
    out = s.explain("layout", user="ana")
    assert out["variant"] == "wide" and out["variant_value"] == {"columns": 3}
    assert s.resolve(user="bob").values["layout"] == "compact"


@pytest.mark.acceptance("LOOP-F17")
def test_a_split_by_weight_is_stable_and_close_to_its_weights(modes):
    split = [{"variant": "compact", "weight": 50}, {"variant": "wide", "weight": 30},
             {"variant": "ratio", "weight": 20}]
    modes["targeting"].put_rollout("layout", "development", rules=[{"split": split}], reason="r", by="@t")
    s = svc()
    users = [f"user-{i}" for i in range(1000)]
    first = {u: s.resolve_flag("layout", user=u) for u in users}
    assert first == {u: s.resolve_flag("layout", user=u) for u in users}
    counts = Counter(first.values())
    assert 430 < counts["compact"] < 570 and 230 < counts["wide"] < 370 and 140 < counts["ratio"] < 260
    out = s.explain("layout", user="user-7")
    assert out["detail"]["split_position"] == t.bucket("layout#split", "user-7")


@pytest.mark.acceptance("LOOP-F17")
def test_a_split_buckets_by_the_rollout_unit(modes):
    modes["targeting"].put_rollout("layout", "development", rules=[{"split": [
        {"variant": "compact", "weight": 1}, {"variant": "wide", "weight": 1}]}], bucket_by="account",
        reason="r", by="@t")
    values = {svc().resolve_flag("layout", user=u, account="acme") for u in ("a", "b", "c", "d", "e")}
    assert len(values) == 1


@pytest.mark.acceptance("LOOP-F17")
def test_a_rule_naming_an_undeclared_variant_or_a_bad_split_is_refused(modes):
    ts = modes["targeting"]
    for rules, match in (
        ([{"value": "tall"}], "variant"),
        ([{"split": [{"variant": "wide", "weight": 0}]}], "weight"),
        ([{"split": [{"variant": "nope", "weight": 1}]}], "variant"),
        ([{"value": "wide", "split": [{"variant": "wide", "weight": 1}]}], "not both"),
    ):
        with pytest.raises(t.RuleError, match=match):
            ts.put_rollout("layout", "development", rules=rules, reason="r", by="@t")
    assert ts.rollout("layout", "development") is None


@pytest.mark.acceptance("LOOP-F17")
def test_overrides_and_the_kill_switch_speak_in_variants(modes):
    s = svc()
    s.set_override("user", "ana", "layout", "wide", by="@t")
    assert s.resolve(user="ana").values["layout"] == {"columns": 3}
    modes["targeting"].set_kill_switch("layout", "development", True, reason="k", by="@t")
    out = s.explain("layout", user="ana")
    assert out["decided_by"] == "kill-switch" and out["variant"] == "compact"


@pytest.mark.acceptance("LOOP-F17")
def test_on_off_soon_flags_are_unchanged(modes):
    modes["targeting"].put_rollout("beta_editor", "development", rules=[{"value": "soon"}], reason="r", by="@t")
    assert svc().resolve_flag("beta_editor", user="ana") == "soon"
    # A split on a plain flag splits its own values.
    modes["targeting"].put_rollout("fast_search", "development", rules=[{"split": [
        {"variant": "on", "weight": 1}, {"variant": "off", "weight": 1}]}], reason="r", by="@t")
    assert {svc().resolve_flag("fast_search", user=f"u{i}") for i in range(40)} == {"on", "off"}


@pytest.mark.acceptance("LOOP-F17")
def test_the_web_sends_the_variant_values_resolved_on_the_server(modes, monkeypatch):
    modes["targeting"].put_rollout("layout", "development", rules=[{"value": "ratio"}], reason="r", by="@t")
    r = web(monkeypatch, sub="ana").get("/api/v1/features")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["flags"]["layout"] == "ratio" and body["values"]["layout"] == 0.5
