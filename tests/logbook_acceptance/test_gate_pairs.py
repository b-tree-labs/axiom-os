# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Gate pairs end to end: proposed at the command line, signed in the web app, applied to the ledger.

ADR-284, settings ledger chunk 3 (prd-settings ST-15; O1 follow-up to #1800). A gate pair ties a
gate role to a signing role, so a person who signs in with the gate role holds the signing role.
It decides who may sign a legal record, so it is an authority key: it changes only by a signed
``AUTHORITY_CHANGE``, and it is read only from the ledger that verifies those records.

The command line cannot prove a fresh sign-in, so it proposes and the person signs where that can
be proved, as for approvals. Signing applies the change.

* LB-GP-A1  a plain settings source cannot supply a gate pair
* LB-GP-A2  a node binds its ledger at discovery, so a signed pair confers its role with no set-up
* LB-GP-A3  proposing a pair writes nothing and says where to sign
* LB-GP-A4  proposing is refused for a person without the role, and for an administration role
* LB-GP-A5  signing the proposal applies it: the pair is listed and confers its signing role
* LB-GP-A6  removing a pair is proposed and signed the same way, and the role stops being conferred
* LB-GP-A7  a pair proposed before another was signed is not applied over it
* LB-GP-A8  a second pair keeps the first
* LB-GP-A9  the proposal waits for the person who proposed it, and for nobody else
* LB-GP-A10 a pair signed for one site does not apply at another
* LB-GP-A11 a record signed at another site is not evidence for this one
* LB-GP-A12 a site id that is not a plain site id reaches no file
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402  # noqa: E402
    grants,
    registry,
    service,
    settings_authority,
    signing,
    site_settings,
)
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest.grants import Authentication  # noqa: E402
from axiom.extensions.builtins.attest.roles import gate_pairs, roles_for  # noqa: E402
from axiom.extensions.builtins.attest.skills import admin  # noqa: E402

from .test_x3 import DESK, SITE, _keys, approver, skill_ctx  # noqa: E402

OWNER = "@op2:site-a"
OPERATOR = "@op1:site-a"
SRO = {"reactor-operator": "reactor_operator"}


@pytest.fixture
def node(attest_db, node_signer, tmp_path, monkeypatch):
    """A node whose settings ledger is bound the way discovery binds it, with one person
    holding the role that signs authority changes."""
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.setattr(settings_authority, "_state_dir", lambda: tmp_path)
    settings_authority.reset_node_ledger()
    registry.register_file(settings_authority.LOGBOOK_FILE)
    signing.set_provider(lambda: node_signer)
    role_store.grant(OWNER, SITE, "site_authority", by="@admin:site-a", state_dir=tmp_path)
    settings_authority.bind()
    yield tmp_path
    settings_authority.uninstall()
    settings_authority.reset_node_ledger()
    site_settings.unregister()
    signing.reset_provider()


def propose(verb, params, tmp_path, *, handle=OWNER):
    fn = {"set": admin.gate_pair_set, "unset": admin.gate_pair_unset}[verb]
    return fn({"site": SITE, **params}, skill_ctx(tmp_path, handle=handle))


def sign(draft_id, signer, *, handle=OWNER, roles=("site_authority",)):
    """The person opens the proposal where a fresh sign-in can be proved, and signs it."""
    who = approver(handle, roles)
    p = service.present(draft_id, modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    auth = Authentication(principal=who.principal, auth_time=datetime.now(UTC) - timedelta(minutes=1))
    token = grants.mint(p.presentation_id, auth=auth, device=DESK, signer=signer)
    return service.sign(p.presentation_id, signatory=who, signer=signer, grant=token, grant_keys=_keys(signer))


def pairs_now(tmp_path: Path) -> tuple[str, ...]:
    return roles_for("@gate-user", SITE, state_dir=tmp_path, gate_roles=tuple(SRO) + ("senior-reactor-operator",))


@pytest.mark.acceptance("LB-GP-A1")
def test_lb_gp_a1_a_plain_settings_source_cannot_supply_a_gate_pair(node):
    site_settings.register(lambda site: {"attest": {"gate_roles": SRO}})

    assert gate_pairs(SITE) == {}
    assert site_settings.authoritative_value(SITE, "attest.gate_roles", "none") == "none"


@pytest.mark.acceptance("LB-GP-A2")
def test_lb_gp_a2_a_node_binds_its_ledger_at_discovery(node, node_signer):
    result = propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node)
    sign(result.value["draft_id"], node_signer)

    site_settings.unregister()  # a fresh process: nothing registered by hand
    settings_authority.reset_node_ledger()
    registry.discover([])

    assert gate_pairs(SITE) == {"reactor-operator": "reactor_operator"}


@pytest.mark.acceptance("LB-GP-A3")
def test_lb_gp_a3_proposing_writes_nothing_and_says_where_to_sign(node):
    result = propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node)

    assert result.ok, result.errors
    assert result.value["draft_id"]
    assert "sign" in " ".join(result.actions_taken).lower()
    assert gate_pairs(SITE) == {}
    assert settings_authority.node_ledger(SITE).verify().records == 0


@pytest.mark.acceptance("LB-GP-A4")
def test_lb_gp_a4_proposing_is_refused_without_the_role_and_for_administration_roles(node):
    nobody = propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node, handle=OPERATOR)
    assert not nobody.ok and "site_authority" in " ".join(nobody.errors)

    for bad in ("node-admin", "admin", "developer"):
        refused = propose("set", {"gate_role": "reactor-operator", "signing_role": bad}, node)
        assert not refused.ok, bad
    admin_gate = propose("set", {"gate_role": "node_admin", "signing_role": "reactor_operator"}, node)
    assert not admin_gate.ok


@pytest.mark.acceptance("LB-GP-A5")
def test_lb_gp_a5_signing_the_proposal_applies_it(node, node_signer):
    assert pairs_now(node) == ()
    result = propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node)

    sign(result.value["draft_id"], node_signer)

    listed = admin.gate_pair_list({"site": SITE}, skill_ctx(node, handle=OPERATOR))
    assert listed.value["pairs"] == {"reactor-operator": "reactor_operator"}
    assert pairs_now(node) == ("reactor_operator",)


@pytest.mark.acceptance("LB-GP-A6")
def test_lb_gp_a6_removing_a_pair_is_signed_the_same_way_and_the_role_stops(node, node_signer):
    sign(propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node).value["draft_id"], node_signer)
    assert pairs_now(node) == ("reactor_operator",)

    removal = propose("unset", {"gate_role": "reactor-operator"}, node)
    assert pairs_now(node) == ("reactor_operator",)  # proposing changes nothing
    sign(removal.value["draft_id"], node_signer)

    assert pairs_now(node) == ()
    assert settings_authority.node_ledger(SITE).value("attest", "gate_roles") is None  # the last pair unsets the key


@pytest.mark.acceptance("LB-GP-A7")
def test_lb_gp_a7_a_proposal_made_before_another_was_signed_is_not_applied_over_it(node, node_signer):
    first = propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node)
    second = propose("set", {"gate_role": "senior-reactor-operator", "signing_role": "senior_reactor_operator"}, node)

    sign(first.value["draft_id"], node_signer)
    sign(second.value["draft_id"], node_signer)  # proposed against the same empty state

    assert gate_pairs(SITE) == {"reactor-operator": "reactor_operator"}  # not silently replaced
    assert settings_authority.node_ledger(SITE).verify().records == 1


@pytest.mark.acceptance("LB-GP-A8")
def test_lb_gp_a8_a_second_pair_keeps_the_first(node, node_signer):
    sign(propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node).value["draft_id"], node_signer)
    sign(propose("set", {"gate_role": "senior-reactor-operator", "signing_role": "senior_reactor_operator"}, node).value["draft_id"], node_signer)

    assert gate_pairs(SITE) == {
        "reactor-operator": "reactor_operator",
        "senior-reactor-operator": "senior_reactor_operator",
    }


@pytest.mark.acceptance("LB-GP-A9")
def test_lb_gp_a9_the_proposal_waits_for_the_person_who_proposed_it_and_nobody_else(node):
    result = propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node)

    mine = service.list_drafts(for_principal=OWNER, site_id=SITE)
    assert [d["draft_id"] for d in mine] == [result.value["draft_id"]]
    assert mine[0]["logbook"] == "settings_authority"
    assert service.list_drafts(for_principal=OPERATOR, site_id=SITE) == []


OTHER_SITE = "site-b"


@pytest.mark.acceptance("LB-GP-A10")
def test_lb_gp_a10_a_pair_signed_for_one_site_does_not_apply_at_another(node, node_signer):
    sign(propose("set", {"gate_role": "reactor-operator", "signing_role": "reactor_operator"}, node).value["draft_id"], node_signer)

    assert gate_pairs(SITE) == SRO
    assert gate_pairs(OTHER_SITE) == {}
    assert roles_for("@gate-user", OTHER_SITE, state_dir=node, gate_roles=("reactor-operator",)) == ()


@pytest.mark.acceptance("LB-GP-A11")
def test_lb_gp_a11_a_record_signed_at_another_site_is_not_evidence_for_this_one(node, node_signer):
    role_store.grant(OWNER, OTHER_SITE, "site_authority", by="@admin:site-b", state_dir=node)
    draft = settings_authority.create_change_draft(
        site_id=OTHER_SITE, section="attest", key="gate_roles", op="set", value=SRO, revision=1, for_principal=OWNER
    )
    evidence = sign(draft, node_signer).record["attestation_id"]

    from axiom.infra import settings_ledger

    with pytest.raises(settings_ledger.AuthorityRefused, match="site-b"):
        settings_authority.node_ledger(SITE).writer("attest", actor=OWNER).set(
            "gate_roles", SRO, expected_revision=0, evidence=evidence
        )
    assert gate_pairs(SITE) == {}


@pytest.mark.acceptance("LB-GP-A12")
def test_lb_gp_a12_a_site_id_that_is_not_a_plain_site_id_reaches_no_file(node):
    for hostile in ("../elsewhere", "a/b", "", "..", "x" * 200):
        assert gate_pairs(hostile) == {}
        with pytest.raises(ValueError):
            settings_authority.node_ledger(hostile)
    assert sorted(p.name for p in node.rglob("ledger.jsonl")) == []
