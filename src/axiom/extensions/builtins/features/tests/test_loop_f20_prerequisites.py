# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F20: a flag may require another flag to resolve to a given value for the same
subject; a cycle is refused; explain shows the chain."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import flag_registry
from axiom.extensions.builtins.features.flag_registry import FlagDef
from axiom.extensions.builtins.features.resolver import ORDER, PREREQUISITE
from axiom.extensions.builtins.features.tests.conftest import svc


@pytest.mark.acceptance("LOOP-F20")
def test_a_flag_whose_prerequisite_is_not_met_resolves_off(modes):
    s = svc()
    s.set_override("user", "ana", "checkout", "on", by="@t")
    out = s.explain("checkout", user="ana")
    assert out["value"] == "off" and out["decided_by"] == PREREQUISITE
    s.set_override("user", "ana", "beta_editor", "on", by="@t")
    out = s.explain("checkout", user="ana")
    assert out["value"] == "on" and out["decided_by"] == "user"


@pytest.mark.acceptance("LOOP-F20")
def test_prerequisites_are_per_subject(modes):
    ts = modes["targeting"]
    ts.put_cohort("staff", principals=["ana"], by="@t")
    ts.put_rollout("beta_editor", "development", rules=[{"cohort": "staff", "value": "on"}], reason="r", by="@t")
    ts.put_rollout("checkout", "development", rules=[{"value": "on"}], reason="r", by="@t")
    s = svc()
    assert s.resolve_flag("checkout", user="ana") == "on"
    assert s.resolve_flag("checkout", user="bob") == "off"


@pytest.mark.acceptance("LOOP-F20")
def test_explain_shows_the_whole_chain(modes):
    ts = modes["targeting"]
    ts.put_rollout("checkout", "development", rules=[{"value": "on"}], reason="r", by="@t")
    ts.put_rollout("checkout_v2", "development", rules=[{"value": "on"}], reason="r", by="@t")
    out = svc().explain("checkout_v2", user="ana")
    assert out["value"] == "off"
    chain = out["prerequisites"]
    assert chain[0]["flag"] == "checkout" and chain[0]["required"] == ["on", "soon"] and not chain[0]["met"]
    assert chain[0]["value"] == "off" and chain[0]["decided_by"] == PREREQUISITE
    inner = chain[0]["prerequisites"][0]
    assert inner["flag"] == "beta_editor" and inner["value"] == "off" and inner["decided_by"] == "default"


@pytest.mark.acceptance("LOOP-F20")
def test_the_prerequisite_step_sits_below_the_kill_switch_and_above_overrides(modes):
    assert ORDER.index("kill-switch") < ORDER.index(PREREQUISITE) < ORDER.index("user")
    modes["targeting"].set_kill_switch("checkout", "development", True, reason="k", by="@t")
    assert svc().explain("checkout", user="ana")["decided_by"] == "kill-switch"


@pytest.mark.acceptance("LOOP-F20")
def test_a_cycle_is_refused_with_its_path(caplog):
    a = FlagDef.from_mapping({"key": "a", "requires": [{"flag": "b", "value": "on"}]})
    b = FlagDef.from_mapping({"key": "b", "requires": [{"flag": "c", "value": "on"}]})
    c = FlagDef.from_mapping({"key": "c", "requires": [{"flag": "a", "value": "on"}]})
    d = FlagDef.from_mapping({"key": "d", "requires": [{"flag": "a", "value": "on"}]})
    built = flag_registry.build_registry([a, b, c, d])
    assert set(built) == {"d"}
    assert "a -> b -> c -> a" in caplog.text
    problems = flag_registry.prerequisite_problems({f.key: f for f in (a, b, c)})
    assert problems and "a -> b -> c -> a" in problems[0]


@pytest.mark.acceptance("LOOP-F20")
def test_a_prerequisite_on_an_undeclared_flag_is_never_met(modes):
    ghost = FlagDef.from_mapping({"key": "needs_ghost", "default": "on",
                                  "requires": [{"flag": "ghost", "value": "on"}]})
    flag_registry.set_registry(list(svc().get_registry().values()) + [ghost])
    out = svc().explain("needs_ghost", user="ana")
    assert out["value"] == "off" and out["prerequisites"][0]["declared"] is False
