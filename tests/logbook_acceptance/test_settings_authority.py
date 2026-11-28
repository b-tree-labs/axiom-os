# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A change to an authority setting is a signed record in the ``settings_authority`` log.

ADR-284 B5 and chunk 2b (prd-settings ST-15). The settings ledger asks a verifier whether
evidence signs one exact change (``test_settings_ledger``, LS-12 to LS-19); this fills that seam
from attest, as ``approvals`` fills the decision seam. Each test carries its id.

* LB-SA-A1  the log loads: one type, one role, and no administration role among its roles
* LB-SA-A2  a person holding the role signs a change, and the ledger accepts it as evidence
* LB-SA-A3  a person without the role cannot sign one
* LB-SA-A4  the verifier refuses a record that is not a settings change, and one for another change
* LB-SA-A5  evidence for one revision cannot be replayed at another
* LB-SA-A6  a signer who later loses the role does not unwind what they signed
* LB-SA-A7  a record whose chain no longer verifies is not evidence
* LB-SA-A8  a pair set this way confers its signing role at the next sign-in
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402  # noqa: E402
    registry,
    service,
    settings_authority,
    signing,
    site_settings,
)
from axiom.extensions.builtins.attest import roles as role_store  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import (  # noqa: E402
    PLATFORM_ADMIN_ROLES,
    load_logbook,
)
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.infra import settings_ledger  # noqa: E402

from .test_x3 import DESK, SITE, _keys, approver  # noqa: E402

# ruff: noqa: F811

OWNER = "@op2:site-a"
OTHER = "@op1:site-a"
PAIRS = {"reactor-operator": "reactor_operator"}
SCHEMA = {"gate_roles": {"type": "object", "additionalProperties": {"type": "string"}}}


def signatory(handle=OWNER, roles=("site_authority",)):
    return approver(handle, roles)


@pytest.fixture
def ledger(attest_db, node_signer, tmp_path, monkeypatch):
    """A ledger whose evidence is checked by attest, with the log registered and one person
    holding the role."""
    monkeypatch.setenv("AXIOM_SITE", SITE)
    registry.register_file(settings_authority.LOGBOOK_FILE)
    signing.set_provider(lambda: node_signer)
    role_store.grant(OWNER, SITE, "site_authority", by="@admin:site-a", state_dir=tmp_path)
    led = settings_ledger.SettingsLedger(tmp_path / "settings.ledger.jsonl", scope=SITE, verification_ttl=0)
    led.declare_section("attest", schema=SCHEMA, authority=("gate_roles",))
    settings_authority.install()
    site_settings.register_authoritative(led.as_site_settings_source())
    yield led
    settings_authority.uninstall()
    site_settings.unregister()
    signing.reset_provider()


def sign_change(*, op="set", value=PAIRS, revision=1, who=None, signer=None, minutes_ago=1):
    """A person is shown a change, and signs it. Returns the attestation id."""
    from datetime import UTC, datetime, timedelta

    from axiom.extensions.builtins.attest import grants
    from axiom.extensions.builtins.attest.grants import Authentication

    who = who or signatory()
    draft = settings_authority.create_change_draft(
        site_id=SITE, section="attest", key="gate_roles", op=op, value=value,
        revision=revision, for_principal=who.principal.handle,
    )  # fmt: skip
    p = service.present(draft, modality="screen")
    service.respond(p.presentation_id, principal=who.principal, answer="sign", via="screen")
    auth = Authentication(
        principal=who.principal, auth_time=datetime.now(UTC) - timedelta(minutes=minutes_ago)
    )
    token = grants.mint(p.presentation_id, auth=auth, device=DESK, signer=signer, )
    return service.sign(
        p.presentation_id, signatory=who, signer=signer, grant=token, grant_keys=_keys(signer)
    ).record["attestation_id"]  # fmt: skip


@pytest.mark.acceptance("LB-SA-A1")
def test_lb_sa_a1_the_log_loads_with_one_type_and_a_role_that_is_not_administration():
    book = load_logbook(settings_authority.LOGBOOK_FILE)

    assert book.id == "settings_authority"
    assert [t.id for t in book.types] == ["AUTHORITY_CHANGE"]
    assert set(book.types[0].roles) == {"site_authority"}
    assert not (set(book.types[0].roles) & PLATFORM_ADMIN_ROLES)
    assert book.shown_in_log is False


@pytest.mark.acceptance("LB-SA-A2")
def test_lb_sa_a2_a_signed_change_is_accepted_by_the_ledger_as_evidence(ledger, node_signer):
    evidence = sign_change(signer=node_signer)

    entry = ledger.writer("attest", actor=OWNER).set(
        "gate_roles", PAIRS, expected_revision=0, evidence=evidence
    )

    assert entry.evidence == evidence
    assert ledger.value("attest", "gate_roles") == PAIRS
    assert ledger.unverified() == []


@pytest.mark.acceptance("LB-SA-A3")
def test_lb_sa_a3_a_person_without_the_role_cannot_sign_a_change(ledger, node_signer):
    with pytest.raises(AttestRefused):
        sign_change(who=signatory(OTHER, roles=()), signer=node_signer)
    assert ledger.value("attest", "gate_roles") is None


@pytest.mark.acceptance("LB-SA-A4")
def test_lb_sa_a4_the_verifier_refuses_what_is_not_a_record_of_this_change(ledger, node_signer):
    w = ledger.writer("attest", actor=OWNER)
    other_value = {"senior-reactor-operator": "senior_reactor_operator"}
    signed_other = sign_change(value=other_value, signer=node_signer)

    with pytest.raises(settings_ledger.AuthorityRefused, match="no signed record"):
        w.set("gate_roles", PAIRS, expected_revision=0, evidence="00000000-0000-0000-0000-000000000000")
    with pytest.raises(settings_ledger.AuthorityRefused, match="different change"):
        w.set("gate_roles", PAIRS, expected_revision=0, evidence=signed_other)
    assert ledger.verify().records == 0


@pytest.mark.acceptance("LB-SA-A5")
def test_lb_sa_a5_evidence_for_one_revision_cannot_be_replayed_at_another(ledger, node_signer):
    w = ledger.writer("attest", actor=OWNER)
    first = sign_change(revision=1, signer=node_signer)
    w.set("gate_roles", PAIRS, expected_revision=0, evidence=first)
    w.unset("gate_roles", expected_revision=1, evidence=sign_change(op="unset", value=None, revision=2, signer=node_signer))

    with pytest.raises(settings_ledger.AuthorityRefused, match="different change"):
        w.set("gate_roles", PAIRS, expected_revision=2, evidence=first)  # would be revision 3
    assert ledger.value("attest", "gate_roles") is None


@pytest.mark.acceptance("LB-SA-A6")
def test_lb_sa_a6_a_signer_who_loses_the_role_does_not_unwind_what_they_signed(ledger, node_signer, tmp_path):
    ledger.writer("attest", actor=OWNER).set(
        "gate_roles", PAIRS, expected_revision=0, evidence=sign_change(signer=node_signer)
    )

    role_store.revoke(OWNER, SITE, "site_authority", by="@admin:site-a", state_dir=tmp_path)

    assert ledger.value("attest", "gate_roles") == PAIRS
    now = signatory(OWNER, roles=role_store.roles_for(OWNER, SITE, state_dir=tmp_path))
    with pytest.raises(AttestRefused):  # but they can sign nothing new
        sign_change(op="unset", value=None, revision=2, who=now, signer=node_signer)


@pytest.mark.acceptance("LB-SA-A7")
def test_lb_sa_a7_a_record_whose_chain_no_longer_verifies_is_not_evidence(ledger, node_signer):
    from sqlalchemy import text

    from axiom.extensions.builtins.attest import store

    evidence = sign_change(signer=node_signer)
    ledger.writer("attest", actor=OWNER).set("gate_roles", PAIRS, expected_revision=0, evidence=evidence)
    assert ledger.value("attest", "gate_roles") == PAIRS

    with store.session_scope() as s:  # someone with the database's own rights edits the record
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(
            text("UPDATE attest_records SET record = jsonb_set(record, '{meaning}', '\"rejected\"') "
                 "WHERE attestation_id = :id"),
            {"id": evidence},
        )  # fmt: skip
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.commit()

    assert ledger.value("attest", "gate_roles") is None
    assert [u.seq for u in ledger.unverified()] == [1]


@pytest.mark.acceptance("LB-SA-A8")
def test_lb_sa_a8_a_pair_set_this_way_confers_its_signing_role(ledger, node_signer, tmp_path):
    from axiom.extensions.builtins.attest.roles import roles_for

    state = Path(tmp_path) / "state"
    state.mkdir()
    assert roles_for("@op", SITE, state_dir=state, gate_roles=("reactor-operator",)) == ()

    ledger.writer("attest", actor=OWNER).set(
        "gate_roles", PAIRS, expected_revision=0, evidence=sign_change(signer=node_signer)
    )

    assert roles_for("@op", SITE, state_dir=state, gate_roles=("reactor-operator",)) == ("reactor_operator",)
