# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit X2: reads of the logbook are recorded, and the chain is checked
on the node heartbeat.

Ticket: docs/working/logbook-units/X2.md in the product repo that holds the plan. The owner's
constraint of 2026-11-10: reads are recorded at the door, one recording path every door calls
(the attest API, the skills chat and MCP use, the CLI, the search provider), each record naming
the principal, the surface, the record ids or range and the labels at the time, never the
content. The doors that exist on main today each have a test here; the PR body lists the ones
that do not exist yet.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from sqlalchemy import text  # noqa: E402

from axiom.attest.chain import seal  # noqa: E402
from axiom.extensions.builtins.attest import integrity, reads, service, store  # noqa: E402
from axiom.extensions.builtins.attest.skills import AUDITED_READS, SPECS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import (  # noqa: E402
    SITE,
    _round_check,
    _sign,
)
from axiom.extensions.builtins.hygiene import node_health  # noqa: E402
from axiom.extensions.builtins.search import engine as search_engine  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import CLI_SURFACE, MCP_SURFACE, invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402

ME = "@op1:site-a"
LOGBOOK = "demo_log"


@pytest.fixture(autouse=True)
def fresh_chain(monkeypatch):
    audit_trail._reset_for_tests()
    reads.reset_failures()
    monkeypatch.setenv("AXIOM_SITE", SITE)
    yield
    audit_trail._reset_for_tests()


@pytest.fixture
def roles(client, tmp_path):
    """The state dir the HTTP client's routes read roles from and write the audit chain to, where
    @op1:site-a is an operator; skills and routes share it, so one chain holds every door's reads."""
    return tmp_path


def ctx(state_dir, *, surface="mcp", handle=ME):
    return SkillContext(
        registry=bind_default(), state_dir=state_dir, logger=logging.getLogger("test.x2"),
        principal=PrincipalContext(handle=handle, posture="sso", assured=True),
        actor=handle, surface=surface,
    )  # fmt: skip


def run(name, params, c, *, surface=MCP_SURFACE):
    return invoke_capability(c.registry, name, params, c, surface=surface)


def chain(state_dir):
    return audit_trail.read_chain(audit_trail.audit_path(state_dir))


def read_records(state_dir):
    """The access records on the chain: the audited read skills and the door helper's own."""
    return [
        r for r in chain(state_dir)
        if r.get("skill") in AUDITED_READS or str(r.get("skill", "")).startswith("attest.read.")
    ]  # fmt: skip


def signed(node_signer, **content):
    return _sign(_round_check(**content), node_signer).record


def keys(node_signer):
    return {node_signer.key_id: node_signer.public_bytes}


# -- acceptance rows -----------------------------------------------------------------------


@pytest.mark.acceptance("LB-X2-A1")
def test_x2_a1_reads_through_the_skills_are_recorded_with_who_when_and_where(
    attest_db, bound_signer, roles, node_signer, tmp_path
):
    rec = signed(node_signer)
    c = ctx(roles)
    assert run("attest.show", {"attestation_id": rec["attestation_id"]}, c).ok
    assert run("attest.show", {"site": SITE, "logbook": LOGBOOK, "limit": 5}, c).ok
    assert run("attest.verify", {"site": SITE, "logbook": LOGBOOK}, c).ok
    out = run("attest.export", {"site": SITE, "logbook": LOGBOOK, "out": str(tmp_path / "pkg")},
              c, surface=CLI_SURFACE)  # fmt: skip
    assert out.ok, out.errors
    recorded = read_records(roles)
    assert [r["skill"] for r in recorded] == [
        "attest.show", "attest.show", "attest.verify", "attest.export"
    ]  # fmt: skip
    for r in recorded:
        assert r["principal"] == ME and r["site"] == SITE
        datetime.fromisoformat(r["ts"])
        assert r["access"]["person"] == ME and r["access"]["logbook"] == LOGBOOK
        assert r["access"]["site"] == SITE and r["access"]["surface"] in ("mcp", "cli")
    assert recorded[0]["access"] == {
        "what": "record", "logbook": LOGBOOK, "site": SITE, "ids": [rec["attestation_id"]],
        "count": 1, "surface": "mcp", "person": ME,
    }  # fmt: skip
    assert recorded[1]["access"]["what"] == "records" and recorded[1]["access"]["count"] == 1
    assert recorded[2]["access"]["what"] == "verify"
    assert recorded[3]["access"]["what"] == "export" and recorded[3]["access"]["count"] == 1
    assert audit_trail.verify_chain(audit_trail.audit_path(roles))


@pytest.mark.acceptance("LB-X2-A2")
def test_x2_a2_reads_through_the_api_are_recorded_with_the_sessions_person(
    client, node_signer, tmp_path
):
    rec = signed(node_signer)
    as_(client, OP1)
    assert client.get(f"/api/v1/attest/{LOGBOOK}/records").status_code == 200
    assert (
        client.get(f"/api/v1/attest/{LOGBOOK}/records/{rec['attestation_id']}").status_code == 200
    )
    assert client.post(f"/api/v1/attest/{LOGBOOK}/verify").status_code == 200
    with client.stream("GET", f"/api/v1/attest/{LOGBOOK}/stream?once=1") as r:
        "".join(r.iter_text())
    recorded = read_records(tmp_path)
    assert [r["skill"] for r in recorded] == [
        "attest.read.records", "attest.read.record", "attest.read.verify", "attest.read.stream"
    ]  # fmt: skip
    for r in recorded:
        assert r["principal"] == ME and r["access"]["surface"] == "web"
        assert r["access"]["logbook"] == LOGBOOK and r["access"]["site"] == SITE
    assert recorded[1]["access"]["ids"] == [rec["attestation_id"]]
    assert recorded[0]["access"]["count"] == 1
    assert audit_trail.verify_chain(audit_trail.audit_path(tmp_path))
    # another person's session names them, not the first reader
    from axiom.extensions.builtins.attest.tests.test_api import OP2

    as_(client, OP2)
    client.get(f"/api/v1/attest/{LOGBOOK}/records")
    assert read_records(tmp_path)[-1]["principal"] == "@op2:site-a"


@pytest.mark.acceptance("LB-X2-A3")
def test_x2_a3_an_audit_write_failure_never_fails_the_read_and_is_counted(
    client, bound_signer, roles, node_signer, tmp_path, monkeypatch
):
    rec = signed(node_signer)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(audit_trail, "locked_append_jsonl", boom)
    as_(client, OP1)
    assert client.get(f"/api/v1/attest/{LOGBOOK}/records").status_code == 200
    assert reads.failures() == 1
    c = ctx(roles)
    r = run("attest.show", {"attestation_id": rec["attestation_id"]}, c)
    assert r.ok and r.value["record"]["attestation_id"] == rec["attestation_id"]
    assert audit_trail.emit_failure_count() >= 1
    assert read_records(roles) == []


@pytest.mark.acceptance("LB-X2-A4")
def test_x2_a4_no_record_content_reaches_the_chain_and_metadata_reads_are_not_recorded(
    attest_db, bound_signer, roles, node_signer, client, tmp_path
):
    rec = signed(node_signer, note="valve left open")
    c = ctx(roles)
    assert run("attest.show", {"attestation_id": rec["attestation_id"]}, c).ok
    assert run("attest.logbook_list", {}, c).ok
    assert run("attest.obligations", {"site": SITE, "logbook": LOGBOOK}, c).ok
    as_(client, OP1)
    client.get(f"/api/v1/attest/{LOGBOOK}/records/{rec['attestation_id']}")
    client.get("/api/v1/attest/logbooks")
    client.get(f"/api/v1/attest/logbooks/{LOGBOOK}")
    for state_dir in (roles, tmp_path):
        blob = audit_trail.audit_path(state_dir).read_text()
        for leak in ("valve left open", "Round check", "walkdown", rec["digest"]):
            assert leak not in blob, leak
        assert rec["attestation_id"] in blob  # the id is the reference, not content
        skills = [r["skill"] for r in chain(state_dir)]
        assert "attest.logbook_list" not in skills and "attest.obligations" not in skills
        assert not any(s.startswith("attest.read.logbook") for s in skills)
    assert set(json.loads(json.dumps(read_records(roles)[0]["access"]))) == {
        "what", "logbook", "site", "ids", "count", "surface", "person"
    }  # fmt: skip


@pytest.mark.acceptance("LB-X2-A5")
def test_x2_a5_a_tampered_chain_and_a_rewritten_head_are_warnings_that_name_the_first_break(
    attest_db, bound_signer, node_signer, tmp_path
):
    signed(node_signer)
    second = signed(node_signer, reading="fine")
    anchor = service.anchor_site(SITE, node_signer)
    # a plain edit of a record the heartbeat has not verified yet: the chain breaks at sequence 2
    with store.session_scope() as s:
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(text("UPDATE attest_records SET record = jsonb_set(record, '{meaning}', '\"approved\"') "
                       "WHERE seq = 2"))  # fmt: skip
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.commit()
    findings = node_health.check_attestation_chains(facts=integrity.check_incremental(tmp_path))
    assert [f.severity for f in findings] == [node_health.Severity.WARNING]
    assert findings[0].check == "attestation_chain"
    assert (
        findings[0].message
        == f"Logbook {LOGBOOK} at {SITE}: the chain breaks at sequence 2 (digest_mismatch)"
    )
    assert (
        findings[0].expected_value == "verified" and findings[0].current_value == "digest_mismatch"
    )
    # a rewrite re-signed with the node key: the chain verifies, the anchor does not
    forged_body = {
        k: v for k, v in second.items() if k not in ("seq", "prev_digest", "digest", "node_sig")
    }
    forged_body["content"] = {**second["content"], "title": "Round check (rewritten)"}
    forged = seal(forged_body, seq=2, prev_digest=second["prev_digest"], signer=node_signer)
    with store.session_scope() as s:
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(text("UPDATE attest_records SET record = CAST(:rec AS jsonb), digest = :d "
                       "WHERE site_id = :site AND logbook = :logbook AND seq = 2"),
                  {"rec": json.dumps(forged), "d": forged["digest"], "site": SITE, "logbook": LOGBOOK})  # fmt: skip
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.execute(text("UPDATE attest_chain_heads SET digest = :d WHERE site_id = :site AND logbook = :logbook"),
                  {"d": forged["digest"], "site": SITE, "logbook": LOGBOOK})  # fmt: skip
        s.commit()
    integrity.forget_verified(tmp_path)
    findings = node_health.check_attestation_chains(facts=integrity.check_incremental(tmp_path))
    assert [f.severity for f in findings] == [node_health.Severity.WARNING]
    assert findings[0].message == (
        f"Logbook {LOGBOOK} at {SITE}: the chain verifies but anchor {anchor['anchor_id']} "
        f"does not (head_rewritten:{LOGBOOK})"
    )


@pytest.mark.acceptance("LB-X2-A6")
def test_x2_a6_the_check_is_incremental_and_a_failed_run_does_not_advance(
    attest_db, bound_signer, roles, node_signer, tmp_path
):
    signed(node_signer)
    signed(node_signer)
    first = integrity.check_incremental(tmp_path)
    assert [(f["logbook"], f["ok"], f["checked"], f["head_seq"]) for f in first] == [
        (LOGBOOK, True, 2, 2)
    ]
    saved = integrity.verified_heads(tmp_path)
    assert saved[SITE][LOGBOOK]["seq"] == 2 and saved[SITE][LOGBOOK]["verified_at"]
    signed(node_signer)
    second = integrity.check_incremental(tmp_path)
    assert second[0]["checked"] == 1 and second[0]["from_seq"] == 3 and second[0]["head_seq"] == 3
    assert integrity.verified_heads(tmp_path)[SITE][LOGBOOK]["seq"] == 3
    assert integrity.check_incremental(tmp_path)[0]["checked"] == 0
    # a record after the saved position is tampered with: the next run finds it and does not advance
    signed(node_signer)
    with store.session_scope() as s:
        s.execute(text("ALTER TABLE attest_records DISABLE TRIGGER attest_records_append_only"))
        s.execute(
            text(
                "UPDATE attest_records SET record = jsonb_set(record, '{meaning}', '\"approved\"') WHERE seq = 4"
            )
        )
        s.execute(text("ALTER TABLE attest_records ENABLE TRIGGER attest_records_append_only"))
        s.commit()
    failed = integrity.check_incremental(tmp_path)
    assert (
        failed[0]["ok"] is False and failed[0]["first_bad_seq"] == 4 and failed[0]["from_seq"] == 4
    )
    assert integrity.verified_heads(tmp_path)[SITE][LOGBOOK]["seq"] == 3, (
        "a failed run never advances"
    )
    assert integrity.check_incremental(tmp_path)[0]["from_seq"] == 4, "and the next run looks again"
    c = ctx(roles, surface="cli")
    everything = run("attest.verify_all", {}, c, surface=CLI_SURFACE)
    assert not everything.ok and everything.value["first_break"]["first_bad_seq"] == 4
    assert everything.value["logbooks"][0]["from_seq"] == 1
    assert everything.value["logbooks"][0]["checked"] == 3
    assert "sequence 4" in everything.errors[0]


@pytest.mark.acceptance("LB-X2-A7")
def test_x2_a7_an_unreadable_store_is_an_info_finding_and_an_empty_node_is_none(
    attest_db, tmp_path, monkeypatch
):
    assert node_health.check_attestation_chains(facts=integrity.check_incremental(tmp_path)) == []

    def unreadable(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(integrity, "check_incremental", unreadable)
    findings = node_health.check_attestation_chains()
    assert [(f.severity, f.check) for f in findings] == [
        (node_health.Severity.INFO, "attestation_chain")
    ]
    assert (
        findings[0].message
        == "Attestation chains were not checked: RuntimeError: connection refused"
    )
    fake = node_health._run

    def quiet(cmd, timeout=10):
        return 1, ""

    report = node_health.audit_node(run=quiet, boot_list_output="x", version_checker=None,
                                    attestation_facts=[])  # fmt: skip
    assert not any(f.check == "attestation_chain" for f in report.findings)
    report = node_health.audit_node(run=quiet, boot_list_output="x", version_checker=None)
    assert [f.severity for f in report.findings if f.check == "attestation_chain"] == [
        node_health.Severity.INFO
    ]
    del fake


@pytest.mark.acceptance("LB-X2-A8")
def test_x2_a8_the_access_log_reads_newest_first_and_verify_all_is_cli_only(
    attest_db, bound_signer, roles, node_signer, tmp_path
):
    by_name = {s.name: s for s in SPECS}
    assert by_name["attest.verify_all"].surfaces == ("cli",)
    assert set(by_name["attest.access"].surfaces) == {"cli", "mcp", "agent_tool"}
    assert by_name["attest.access"].side_effects is False
    rec = signed(node_signer)
    c = ctx(roles)
    run("attest.show", {"site": SITE, "logbook": LOGBOOK}, c)
    run("attest.show", {"attestation_id": rec["attestation_id"]}, c)
    before = len(chain(roles))
    access = run("attest.access", {"logbook": LOGBOOK, "since": "1h"}, c)
    assert access.ok, access.errors
    assert [a["what"] for a in access.value["reads"]] == ["record", "records"]
    assert (
        access.value["reads"][0]["principal"] == ME and access.value["reads"][0]["surface"] == "mcp"
    )
    assert access.value["reads"][0]["ids"] == [rec["attestation_id"]]
    assert len(chain(roles)) == before, "reading the access log is not itself recorded"
    other = run("attest.access", {"logbook": "another_log"}, c)
    assert other.ok and other.value["reads"] == []


# -- every door that exists on main today -------------------------------------------------


def test_x2_every_door_on_main_records_the_same_shape(
    attest_db, bound_signer, roles, node_signer, client, tmp_path, monkeypatch
):
    """The owner's rule: one recording path, called at every door. API (A2) and MCP (A1) above;
    here the CLI surface, the chat surface and the search provider."""
    rec = signed(node_signer)
    cli = ctx(roles, surface="cli")
    assert run(
        "attest.show", {"attestation_id": rec["attestation_id"]}, cli, surface=CLI_SURFACE
    ).ok
    chat = ctx(roles, surface="chat")
    assert run("attest.show", {"site": SITE, "logbook": LOGBOOK}, chat, surface="chat").ok
    import axiom.infra.paths as paths

    monkeypatch.setattr(paths, "get_user_state_dir", lambda: roles)
    hits = search_engine.logbooks(
        "round", search_engine.Scope(principal=ME, sites=frozenset({SITE})), 10
    )
    assert [h.id for h in hits] == [rec["attestation_id"]]
    recorded = read_records(roles)
    assert [(r["access"]["surface"], r["access"]["what"]) for r in recorded] == [
        ("cli", "record"), ("chat", "records"), ("search", "search")
    ]  # fmt: skip
    assert all(set(r["access"]) == {"what", "logbook", "site", "ids", "count", "surface", "person"}
               for r in recorded)  # fmt: skip
    assert recorded[2]["principal"] == ME and recorded[2]["access"]["logbook"] == LOGBOOK
    assert audit_trail.verify_chain(audit_trail.audit_path(roles))


def test_x2_the_chain_check_is_a_platform_event_for_the_journal(
    attest_db, bound_signer, node_signer, tmp_path
):
    from axiom.infra import bus as bus_mod
    from axiom.infra.bus import EventBus

    b = EventBus()
    seen: list[tuple[str, dict]] = []
    b.subscribe("health.>", lambda s, p: seen.append((s, p)), source="t")
    bus_mod.set_default_eventbus(b)
    try:
        signed(node_signer)
        integrity.check_incremental(tmp_path)
    finally:
        bus_mod.set_default_eventbus(None)
    assert [s for s, _ in seen] == ["health.attestation_chain.ok"]
    assert (
        seen[0][1]["logbook"] == LOGBOOK
        and seen[0][1]["site_id"] == SITE
        and seen[0][1]["schema_version"] == 1
    )
    assert "content" not in json.dumps(seen[0][1])
    assert datetime.now(UTC).year >= 2026


def test_x2_the_cli_has_access_and_verify_all():
    from axiom.extensions.builtins.attest import cli, log_cli

    noun = log_cli.noun_parser()
    assert noun.parse_args(["verify-all"]).verb == "verify-all"
    assert noun.parse_args(["access", "--since", "7d"]).since == "7d"
    assert log_cli.log_parser("demo").parse_args(["access", "--since", "7d"]).since == "7d"
    # the old forms still reach them (ADR-243), and `verify` with no log is still a usage error
    assert cli.translate(["verify", "--all"]) == ["verify-all"]
    assert cli.translate(["access", "--logbook", LOGBOOK, "--since", "7d"])[1:] == ["access", "--since", "7d"]
    assert cli.main(["verify"]) == 2
