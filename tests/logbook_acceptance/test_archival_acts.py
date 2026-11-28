# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Archival acts (ADR-290 D8): a log of either kind seals, archives and deletes under its declared rule.

A seal is signed by the accountable human where one can be traced to the actor, else by the actor;
declared accountability is the authority. The log's entries stay plain: archiving is no reason to
sign them.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")

from axiom.attest import verify as verify_lib  # noqa: E402
from axiom.extensions.builtins.attest import registry, service  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import SITE  # noqa: E402

from .test_plain import decl, parse, write  # noqa: E402
from .test_plain import logs as _logs  # noqa: E402

logs = _logs  # the plain-log fixture, by the name its tests use
from axiom.extensions.builtins.attest.logbooks import parse_logbook  # noqa: E402

KEPT = "kept_notes"
SEAL = '[seal]\nby_roles = ["lead"]\nboundary = "range"\n'
RETENTION = 'retention = { delete_after_days = 2000, tombstone_by = ["lead"] }\n'


def kept(log_id=KEPT, **kw):
    kw.setdefault("signed", False)  # a plain log that archives says so: that is what makes it plain
    return parse_logbook(decl(log_id, extra_head=RETENTION, extra=SEAL, **kw), source="archival-test")


@pytest.fixture(autouse=True)
def no_site_config(monkeypatch):
    monkeypatch.delenv("AXIOM_AGENT_ACCOUNTABLE", raising=False)


@pytest.fixture
def kept_log(logs):
    registry.register(kept())
    for i in range(1, 4):
        write(log=KEPT, text=f"entry {i}")
    return logs


def seal(**kw):
    args = dict(site_id=SITE, logbook=KEPT, summary="the first three entries", actor="@lee:site-a",
                actor_kind="human", roles=("lead",), boundary="range", from_seq=1, to_seq=3)  # fmt: skip
    args.update(kw)
    return service.record_seal(**args)


# -- the declaration: archiving is no reason to sign entries ---------------------------------------


@pytest.mark.acceptance("LB-AA-A1")
def test_aa_a1_a_log_that_says_signed_false_may_declare_a_seal_and_an_archiving_rule_and_stay_plain():
    lb = kept()
    assert lb.signed is False and lb.signed_declared is True and lb.signed_by_inference is False
    assert lb.seal is not None and lb.retention_rule is not None and lb.retention_rule.deletes
    assert kept("kept_signed", signed=True).signed is True
    # a declaration that says nothing and declares them keeps what it meant before: signed
    said_nothing = parse_logbook(decl("kept_silent", extra_head=RETENTION, extra=SEAL), source="archival-test")
    assert said_nothing.signed is True and said_nothing.signed_by_inference is True
    assert "a seal" in said_nothing.signed_because


@pytest.mark.acceptance("LB-AA-A2")
def test_aa_a2_a_log_that_became_plain_by_default_says_so_when_it_loads(caplog):
    with caplog.at_level(logging.WARNING, logger="axiom.extensions.builtins.attest.registry"):
        registry.register(parse("by_default_log"))
        registry.register(parse("said_plain_log", signed=False))
    said = [r.getMessage() for r in caplog.records]
    assert any("by_default_log" in m and "plain" in m and "signed = true" in m for m in said), said
    assert not any("said_plain_log" in m for m in said)


# -- the seal act -----------------------------------------------------------------------------


@pytest.mark.acceptance("LB-AA-A3")
def test_aa_a3_a_person_seals_a_plain_log_in_one_step_and_the_chain_holds(kept_log):
    rec = seal()
    assert rec["kind"] == "seal" and rec["meaning"] == "sealed" and rec["seq"] == 4
    assert rec["content"]["seal"]["covers"]["from_seq"] == 1 and rec["content"]["seal"]["covers"]["to_seq"] == 3
    assert rec["signer"] == {"principal": "@lee:site-a", "kind": "self", "authority": {"roles": ["lead"]}} or (
        rec["signer"]["principal"] == "@lee:site-a" and rec["signer"]["kind"] == "self"
    )
    assert "evidence" not in rec and rec["content"]["summary"] == "the first three entries"
    keys = {kept_log.key_id: kept_log.public_bytes}
    report = verify_lib.chain(SITE, KEPT, 1, 4, public_keys=keys)
    assert report.ok and report.checked == 4
    assert len(service.anchors(SITE)) == 1  # an anchor at every seal


@pytest.mark.acceptance("LB-AA-A4")
def test_aa_a4_a_person_without_the_seals_role_is_refused_and_a_period_is_sealed_once(kept_log):
    with pytest.raises(service.AttestRefused, match="holds no role"):
        seal(roles=("visitor",))
    seal()
    with pytest.raises(service.AttestRefused, match="already sealed"):
        seal()
    with pytest.raises(service.AttestRefused, match="summary"):
        seal(summary="  ", from_seq=1, to_seq=2)
    with pytest.raises(service.AttestRefused, match="declares no seal"):
        service.record_seal(site_id=SITE, logbook="plain_notes", summary="x", actor="@lee:site-a",
                            actor_kind="human", roles=("lead",), boundary="range", from_seq=1, to_seq=1)  # fmt: skip
    assert [r["kind"] for r in service.records(SITE, KEPT)].count("seal") == 1


@pytest.mark.acceptance("LB-AA-A5")
def test_aa_a5_an_agent_seals_as_the_human_the_site_names_with_no_role_check(kept_log, monkeypatch):
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "keel=@sam:site-a")
    rec = seal(actor="@keel:site-a", actor_kind="agent", roles=(), summary="Routine close-out by schedule")
    assert rec["signer"]["principal"] == "@sam:site-a" and rec["signer"]["kind"] == "accountable"
    assert rec["signer"]["performed_by"] == "@keel:site-a" and rec["signer"]["accountable_source"] == "site"
    assert "authority" not in rec["signer"]  # declared accountability is the authority; no role is claimed


@pytest.mark.acceptance("LB-AA-A6")
def test_aa_a6_a_machine_with_no_human_traced_seals_as_itself(kept_log):
    rec = seal(actor="@node1:site-a", actor_kind="machine", roles=(), summary="Routine close-out")
    assert rec["signer"]["principal"] == "@node1:site-a" and rec["signer"]["kind"] == "actor"
    assert rec["signer"]["performed_by"] == "@node1:site-a"


@pytest.mark.acceptance("LB-AA-A7")
def test_aa_a7_a_signed_log_takes_the_same_act_and_a_log_may_not_seal_a_range_past_its_head(kept_log):
    registry.register(kept("kept_signed", signed=True))
    # the same act, on a signed log: its entries come through drafts, its close-out may be this act
    from axiom.extensions.builtins.attest.tests.test_signing import _sign

    d = service.create_draft(site_id=SITE, logbook="kept_signed", entry_type="NOTE", meaning="authored",
                             content={"title": "s", "fields": {"text": "s"}}, origin="human",
                             for_principal="@op1:site-a")  # fmt: skip
    _sign(d, kept_log)
    rec = service.record_seal(site_id=SITE, logbook="kept_signed", summary="one entry", actor="@lee:site-a",
                              actor_kind="human", roles=("lead",), boundary="range", from_seq=1, to_seq=1)  # fmt: skip
    assert rec["kind"] == "seal"
    with pytest.raises(service.AttestRefused, match="past the chain head"):
        seal(from_seq=1, to_seq=99)


# -- the whole cycle on a plain log: seal, archive, tombstone, erase ------------------------------


@pytest.fixture
def cycle(kept_log, tmp_path, monkeypatch):
    """A plain log with a seal and a deletion rule: three entries, sealed by a person, archived."""
    from datetime import UTC, datetime, timedelta

    import axiom.infra.paths as paths
    from axiom.extensions.builtins.attest import archives, retention
    from axiom.extensions.builtins.attest import roles as role_table
    from axiom.infra.storage import provider_from_config

    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    role_table.grant("@lee:site-a", SITE, "lead", by="@admin:site-a", state_dir=tmp_path)
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    archives.set_target(provider_from_config({"base_dir": str(tmp_path / "cold")}))
    sealed = seal()
    built = archives.build(SITE, KEPT, by="@lee:site-a")
    archives.verify(built["archive_id"], by="@lee:site-a")
    archives.commit(built["archive_id"], owner="@lee:site-a")
    archives.retire(built["archive_id"], approver="@lee:site-a", override_soak="test: no soak")
    monkeypatch.setattr(retention, "_now", lambda: datetime.now(UTC) + timedelta(days=2100))
    yield {"seal": sealed, "archive_id": built["archive_id"], "signer": kept_log}
    archives.set_target(None)
    archives._packages.clear()  # noqa: SLF001


@pytest.mark.acceptance("LB-AA-A8")
def test_aa_a8_a_plain_log_is_archived_and_a_person_removes_the_period_by_the_rule(cycle):
    from axiom.extensions.builtins.attest import retention

    with pytest.raises(service.AttestRefused, match="holds no role"):
        service.record_tombstone(site_id=SITE, logbook=KEPT, archive_id=cycle["archive_id"],
                                 actor="@lee:site-a", actor_kind="human", roles=("visitor",))  # fmt: skip
    t = service.record_tombstone(site_id=SITE, logbook=KEPT, archive_id=cycle["archive_id"],
                                 actor="@lee:site-a", actor_kind="human", roles=("lead",))  # fmt: skip
    assert t["kind"] == "tombstone" and t["target"] == cycle["seal"]["attestation_id"]
    assert t["signer"]["principal"] == "@lee:site-a" and t["signer"]["kind"] == "self"
    with pytest.raises(service.AttestRefused, match="already has"):
        service.record_tombstone(site_id=SITE, logbook=KEPT, archive_id=cycle["archive_id"],
                                 actor="@lee:site-a", actor_kind="human", roles=("lead",))  # fmt: skip
    retention.commit(t["attestation_id"], owner="@lee:site-a")
    retention.erase(t["attestation_id"], approver="@lee:site-a", override_soak="test: no soak")
    keys = {cycle["signer"].key_id: cycle["signer"].public_bytes}
    report = verify_lib.chain(SITE, KEPT, 1, len(service.records(SITE, KEPT)), public_keys=keys)
    assert report.ok, report


@pytest.mark.acceptance("LB-AA-A9")
def test_aa_a9_a_machine_removes_the_period_signed_as_the_human_the_site_names_and_only_by_the_rule(
    cycle, monkeypatch
):
    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "*=@sam:site-a")
    t = service.record_tombstone(site_id=SITE, logbook=KEPT, archive_id=cycle["archive_id"],
                                 actor="@node1:site-a", actor_kind="machine")  # fmt: skip
    assert t["signer"]["principal"] == "@sam:site-a" and t["signer"]["kind"] == "accountable"
    assert t["signer"]["performed_by"] == "@node1:site-a" and t["signer"]["accountable_source"] == "site-default"
    # a log with no deletion rule: nobody, machine or person, removes content from it
    registry.register(kept("no_rule_log"))
    registry.register(parse("no_rule_plain"))
    with pytest.raises(service.AttestRefused, match="declares no rule"):
        service.record_tombstone(site_id=SITE, logbook="no_rule_plain", archive_id="x",
                                 actor="@node1:site-a", actor_kind="machine")  # fmt: skip


@pytest.mark.acceptance("LB-AA-A10")
def test_aa_a10_the_seal_verb_closes_out_a_plain_log_for_the_person_at_the_command_line(kept_log, tmp_path):
    import logging as _logging

    from axiom.extensions.builtins.attest import roles as role_table
    from axiom.extensions.builtins.attest.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext

    role_table.grant("@lee:site-a", SITE, "lead", by="@admin:site-a", state_dir=tmp_path)
    ctx = SkillContext(
        registry=bind_default(), state_dir=tmp_path, logger=_logging.getLogger("test.aa"),
        principal=PrincipalContext(handle="@lee:site-a", posture="sso", assured=True),
        actor="@lee:site-a", surface=CLI_SURFACE, user_prompt=lambda text: "sign",  # a terminal; never asked
    )  # fmt: skip
    params = {"site": SITE, "logbook": KEPT, "summary": "the first three", "boundary": "range",
              "from_seq": 1, "to_seq": 3}  # fmt: skip
    done = invoke_capability(ctx.registry, "attest.seal", params, ctx, surface=CLI_SURFACE)
    assert done.ok, done.errors
    assert done.value["record"]["kind"] == "seal" and done.value["record"]["signer"]["kind"] == "self"
    again = invoke_capability(ctx.registry, "attest.seal", params, ctx, surface=CLI_SURFACE)
    assert not again.ok and "already sealed" in " ".join(again.errors)


@pytest.mark.acceptance("LB-AA-A11")
def test_aa_a11_a_machine_approves_the_erase_by_the_declared_rule_a_person_still_needs_the_role(
    cycle, monkeypatch
):
    """Erase follows the same rule as the tombstone: an agent or machine is held to the declared rule
    (and the hold and the platform floor), not to a role-table entry; a person is held to the role."""
    from axiom.extensions.builtins.attest import retention

    monkeypatch.setenv("AXIOM_AGENT_ACCOUNTABLE", "*=@sam:site-a")
    t = service.record_tombstone(site_id=SITE, logbook=KEPT, archive_id=cycle["archive_id"],
                                 actor="@lee:site-a", actor_kind="human", roles=("lead",))  # fmt: skip
    retention.commit(t["attestation_id"], owner="@lee:site-a")
    with pytest.raises(service.AttestRefused, match="must hold a tombstone role"):
        retention.erase(t["attestation_id"], approver="@visitor:site-a", override_soak="test: no soak")
    done = retention.erase(t["attestation_id"], approver="@node1:site-a", approver_kind="machine",
                           override_soak="test: no soak")  # fmt: skip
    assert done["status"] != "committed"  # the erasure ran
