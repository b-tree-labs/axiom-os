# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LOOP-F3: attributes come from a catalogue; an attribute with no data is UNKNOWN, and
UNKNOWN never admits a member, even through ``not``."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import attributes
from axiom.extensions.builtins.features import targeting as t
from axiom.extensions.builtins.features.tests.conftest import svc

PLATFORM = {"role", "site", "account", "account_kind", "node", "node_role", "environment",
            "principal", "principal_kind", "locale"}


@pytest.mark.acceptance("LOOP-F3")
def test_the_platform_declares_the_common_attributes(loop):
    assert set(attributes.catalogue()) >= PLATFORM
    for name in PLATFORM:
        assert attributes.catalogue()[name].description


@pytest.mark.acceptance("LOOP-F3")
def test_platform_attributes_resolve_what_the_platform_knows(loop, monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", "north")
    monkeypatch.setenv("AXIOM_ENVIRONMENT", "staging")
    subject = attributes.Subject(principal="ana", account="acme", node="node-a",
                                 roles=("site-admin",), locale="fr-CA", principal_kind="person")
    look = attributes.lookup_for(subject)
    assert look("principal").value == "ana"
    assert look("account").value == "acme"
    assert look("site").value == "north"
    assert look("environment").value == "staging"
    assert look("node").value == "node-a"
    # A retired id rides beside its successor, so a stored rule naming either matches (ADR-261).
    assert look("role").value == ["site-admin", "operator"]
    assert look("locale").value == "fr-CA"
    assert look("principal_kind").value == "person"
    # Nothing in the platform knows an account's kind: no resolver, so UNKNOWN.
    assert look("account_kind").available is False


@pytest.mark.acceptance("LOOP-F3")
def test_unknown_never_admits_even_through_not(loop):
    ts = loop["targeting"]
    ts.put_cohort("not_viewers", rules={"not": {"attr": "role", "op": "contains", "value": "viewer"}}, by="@t")
    ts.put_cohort("kind_neq", rules={"attr": "account_kind", "op": "neq", "value": "trial"}, by="@t")
    ts.put_rollout("beta_editor", "development",
                   rules=[{"cohort": "not_viewers", "value": "on"}, {"cohort": "kind_neq", "value": "on"}],
                   reason="r", by="@t")
    s = svc()
    # No roles known for this caller: `not role contains viewer` is UNKNOWN, not True.
    out = s.explain("beta_editor", user="ana", account="acme")
    assert out["value"] == "off" and out["decided_by"] == "default"
    assert set(out["unknown_attributes"]) == {"role", "account_kind"}
    # Known roles decide it.
    assert s.resolve_flag("beta_editor", user="ana", roles=["site-admin"]) == "on"


@pytest.mark.acceptance("LOOP-F3")
def test_an_extension_declares_an_attribute_with_a_resolver_in_its_manifest(loop):
    manifest = {"flag_attributes": [
        {"name": "plan_tier", "description": "the account's plan", "resolver": f"{__name__}:_tier"},
        {"name": "role", "description": "shadows the platform", "resolver": f"{__name__}:_tier"},
        {"name": "Bad Name", "resolver": f"{__name__}:_tier"},
    ]}
    declared = attributes.parse_manifest_attributes(manifest, owner="billing_ext")
    assert [a.name for a in declared] == ["plan_tier", "role"]
    attributes.set_extension_attributes(declared)
    cat = attributes.catalogue()
    assert cat["plan_tier"].owner == "billing_ext"
    assert cat["role"].owner == ""  # a platform attribute is never shadowed
    look = attributes.lookup_for(attributes.Subject(principal="ana", account="gold-co"))
    assert look("plan_tier").value == "gold"
    assert look("plan_tier").available is True
    look = attributes.lookup_for(attributes.Subject(principal="ana", account=None))
    assert look("plan_tier").value is None  # the resolver had no data for this subject


@pytest.mark.acceptance("LOOP-F3")
def test_a_resolver_that_fails_reads_unknown(loop):
    attributes.set_extension_attributes(
        [attributes.AttributeDef(name="flaky", description="raises", resolver=_boom, owner="x")]
    )
    rule = {"attr": "flaky", "op": "eq", "value": 1}
    res = t.evaluate(rule, attributes.lookup_for(attributes.Subject(principal="ana")))
    assert res.value is None and res.unknown == ["flaky"]


def _tier(subject):
    return "gold" if subject.account == "gold-co" else None


def _boom(subject):
    raise RuntimeError("down")
