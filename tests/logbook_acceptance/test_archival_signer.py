# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who signs an archival act (ADR-290 D8): the accountable human where one can be traced, else the actor.

Declared accountability is the authority; a human name tied to the actor is enough. No database.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.attest import accountability as acc


@pytest.fixture(autouse=True)
def no_site_config(monkeypatch):
    monkeypatch.delenv("AXIOM_AGENT_ACCOUNTABLE", raising=False)


@pytest.mark.acceptance("LB-AS-A1")
def test_as_a1_a_human_acting_signs_for_themselves():
    got = acc.resolve("@pat:site-a", "human")
    assert (got.signer, got.performed_by, got.kind, got.source) == ("@pat:site-a", "@pat:site-a", "self", "self")
    assert got.traced


@pytest.mark.acceptance("LB-AS-A2")
def test_as_a2_an_agent_is_signed_for_by_the_human_the_site_names(monkeypatch):
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "keel=@lee:site-a")
    got = acc.resolve("@keel:site-a", "agent")
    assert got.signer == "@lee:site-a" and got.performed_by == "@keel:site-a"
    assert got.kind == "accountable" and got.source == "site" and got.traced


@pytest.mark.acceptance("LB-AS-A3")
def test_as_a3_the_activation_record_and_the_agents_declaration_trace_too():
    record = {"agents": {"keel": {"active": True, "accountable": "@sam:site-a"}}}
    got = acc.resolve("@keel:site-a", "agent", record=record)
    assert got.signer == "@sam:site-a" and got.source == "record"
    declared = acc.resolve("@rivet:site-a", "agent", record={"agents": {}}, declared={"rivet": "@max:site-a"})
    assert declared.signer == "@max:site-a" and declared.source == "declaration"


@pytest.mark.acceptance("LB-AS-A4")
def test_as_a4_a_machine_is_signed_for_by_the_site_default_human(monkeypatch):
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "*=@lee:site-a")
    got = acc.resolve("@node1:site-a", "machine")
    assert got.signer == "@lee:site-a" and got.performed_by == "@node1:site-a"
    assert got.kind == "accountable" and got.source == "site-default"


@pytest.mark.acceptance("LB-AS-A5")
def test_as_a5_with_no_human_traced_the_agent_or_machine_signs_as_itself():
    for actor, kind in (("@keel:site-a", "agent"), ("@node1:site-a", "machine")):
        got = acc.resolve(actor, kind)
        assert (got.signer, got.performed_by, got.kind, got.source) == (actor, actor, "actor", "")
        assert not got.traced


@pytest.mark.acceptance("LB-AS-A6")
def test_as_a6_a_named_agent_beats_the_site_default_and_the_chain_above_is_kept_when_known(monkeypatch):
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "*=@lee:site-a,keel=@sam:site-a")
    assert acc.resolve("@keel:site-a", "agent").signer == "@sam:site-a"
    assert acc.resolve("@other:site-a", "agent").signer == "@lee:site-a"
    # more of the chain is recorded when known and never required
    got = acc.resolve("@keel:site-a", "agent", answers_to="@pat:site-a")
    assert got.answers_to == "@pat:site-a" and acc.resolve("@keel:site-a", "agent").answers_to == ""


@pytest.mark.acceptance("LB-AS-A7")
def test_as_a7_the_record_fields_say_who_performed_it_and_how_it_was_traced():
    got = acc.resolve("@keel:site-a", "agent", record={"agents": {"keel": {"accountable": "@lee:site-a"}}})
    assert got.as_record() == {
        "principal": "@lee:site-a", "kind": "accountable", "performed_by": "@keel:site-a",
        "accountable_source": "record",
    }
    assert acc.resolve("@pat:site-a", "human").as_record() == {"principal": "@pat:site-a", "kind": "self"}
    assert acc.resolve("@keel:site-a", "agent").as_record() == {
        "principal": "@keel:site-a", "kind": "actor", "performed_by": "@keel:site-a",
    }
