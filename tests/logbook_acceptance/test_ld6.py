# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-6: retention enforcement with tombstones (#1763, ADR-251 D7).

The world is LD-5's: record #1 (LD-1's), record #2 carrying two attachments (a photo and a
second file only it names), the range seal #3 over both, archived, committed and retired. Then
record #4, after the seal, names the same photo, so the photo is still a live blob after the
period goes. The demo log is given a retention rule for the test and put back after; the clock
the retention rule reads is moved past the rule's age instead of waiting a year.

The flow under test: a rule declared on the log; a dry run naming everything that would go; a
tombstone proposed (by software or a person) and signed by a person in the role the rule names;
the erasure on the ADR-197 runner (commit hides, a named approver's retire destroys); then the
chain still verifies, the covered records keep digest, signatures and labels and lose content,
their blobs, index rows, fragments and journal text go, and the archived package goes with them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

fastapi = pytest.importorskip("fastapi")

from axiom.attest.chain import verify_chain  # noqa: E402
from axiom.extensions.builtins.attest import (  # noqa: E402
    archives,
    attachments,
    gold,
    index,
    registry,
    retention,
    service,
    site_settings,
    store,
)
from axiom.extensions.builtins.attest import references as log_refs  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_retention  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.extensions.builtins.attest.skills import bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import _sign, person  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.infra.switchplan import SwitchRefused  # noqa: E402

from . import test_ld1 as ld1  # noqa: E402
from . import test_ld4 as ld4  # noqa: E402
from . import test_ld5 as ld5  # noqa: E402
from .test_j1 import journal_db  # noqa: E402,F401
from .test_ld1 import LOG, ME, SECRET_NOTE, SITE  # noqa: E402
from .test_ld2 import ids as ld2_ids  # noqa: E402

world = ld1.world
restore_log = ld1.restore_log
blob_store = ld4.blob_store

CUSTODIAN = "records_custodian"
SECOND = b"ld6 second file only the covered record names " * 6
RULE = {"live_days": 30, "archive": "site", "class": "system", "delete_after_days": 365,
        "tombstone_by": [CUSTODIAN]}  # fmt: skip


def custodian(roles=(CUSTODIAN,)):
    return person(roles=roles)


def _ruled(rule=RULE, **over):
    """The demo log, sealable by range (LD-5), with ``rule`` as its declared retention."""
    ld5._sealable()
    book = registry.get(LOG)
    registry.register(replace(book, retention=rule, retention_rule=parse_retention(rule, LOG), **over))


def _past_due(monkeypatch, days: int = 400) -> None:
    monkeypatch.setattr(retention, "_now", lambda: datetime.now(UTC) + timedelta(days=days))


@pytest.fixture
def ld6(world, restore_log, blob_store, node_signer, tmp_path, monkeypatch):
    """Records #1 and #2 (two files), sealed by #3, archived and retired; #4 names the photo."""
    _ruled()
    from axiom.extensions.builtins.attest import roles

    roles.grant(ME, SITE, CUSTODIAN, by="@admin:site-a", state_dir=tmp_path)
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    cold = tmp_path / "cold"
    from axiom.infra.storage import provider_from_config

    archives.set_target(provider_from_config({"base_dir": str(cold)}))
    rec2 = ld4._signed(node_signer, (ld4.PHOTO, "image/png", {"name": "pump.png"}),
                       (SECOND, "text/plain", {"name": "second.txt"}), note="the second covered entry")  # fmt: skip
    draft = service.create_seal_draft(site_id=SITE, logbook=LOG, boundary="range", from_seq=1, to_seq=2,
                                      summary="Two rounds.", origin="human", for_principal=ME)  # fmt: skip
    seal = _sign(draft, node_signer).record
    built = archives.build(SITE, LOG, by=ME)
    archives.verify(built["archive_id"], by=ME)
    archives.commit(built["archive_id"], owner=ME)
    archives.retire(built["archive_id"], approver=ME, override_soak="test: no soak")
    rec4 = ld4._signed(node_signer, (ld4.PHOTO, "image/png", {"name": "again.png"}))
    shas = {r["name"]: r["sha256"] for r in attachments.refs_of(rec2)}
    index.refresh([(SITE, LOG)], state_dir=tmp_path)
    yield {**world, "rec2": rec2, "seal": seal, "rec4": rec4, "archive_id": built["archive_id"],
           "package_digest": built["package_digest"], "cold": cold, "photo": shas["pump.png"],
           "second": shas["second.txt"], "tmp": tmp_path, "signer": node_signer}  # fmt: skip
    archives.set_target(None)
    archives._packages.clear()  # noqa: SLF001


def _tombstone(w, *, origin="human", who=None):
    draft = service.create_tombstone_draft(site_id=SITE, logbook=LOG, archive_id=w["archive_id"],
                                           origin=origin, for_principal=ME)  # fmt: skip
    return _sign(draft, w["signer"], who=who or custodian())


def _erased(w, monkeypatch):
    """A tombstone signed, committed and retired: the period's content is gone."""
    _past_due(monkeypatch)
    t = _tombstone(w).record
    retention.commit(t["attestation_id"], owner=ME)
    retention.erase(t["attestation_id"], approver=ME, override_soak="test: no soak")
    return t


def _chain_rows(site=SITE, logbook=LOG):
    with store.session_scope() as s:
        return {int(r.seq): dict(r.record) for r in s.execute(text(
            "SELECT seq, record FROM attest_records WHERE site_id = :s AND logbook = :l ORDER BY seq"),
            {"s": site, "l": logbook})}  # fmt: skip


def _keys(w):
    return {w["signer"].key_id: w["signer"].public_bytes}


def _ctx(state: Path, handle: str | None, surface: str = "cli") -> SkillContext:
    principal = PrincipalContext(handle=handle, posture="sso", assured=True) if handle else None
    return SkillContext(registry=bind_default(), state_dir=state, logger=logging.getLogger("ld6"),
                        principal=principal, actor=handle or "", surface=surface)  # fmt: skip


def _skill(w, name, params, handle=ME, surface="cli"):
    c = _ctx(w["state"], handle, surface)
    return invoke_capability(c.registry, name, params, c, surface=surface)


# -- LD-6-A1: no declared retention, nothing deleted ---------------------------------------------


@pytest.mark.acceptance("LD-6-A1")
def test_ld6_a1_a_log_with_no_retention_never_has_anything_deleted(ld6, monkeypatch):
    book = registry.get(LOG)
    registry.register(replace(book, retention=None, retention_rule=None))
    _past_due(monkeypatch, days=365 * 50)
    with pytest.raises(AttestRefused, match="declares no retention"):
        retention.preview(SITE, LOG, archive_id=ld6["archive_id"])
    with pytest.raises(AttestRefused, match="declares no retention"):
        service.create_tombstone_draft(site_id=SITE, logbook=LOG, archive_id=ld6["archive_id"],
                                       origin="human", for_principal=ME)  # fmt: skip
    report = retention.report(SITE, LOG)
    assert report["declared"] is None and report["effective"]["deletes"] is False
    assert report["next_due"]["delete"] is None
    body = as_(ld6["client"], OP1).get(f"/api/v1/attest/logbooks/{LOG}").json()
    assert body["retention"] is None
    assert all(r.get("content") for r in _chain_rows().values())


@pytest.mark.acceptance("LD-6-A1")
def test_ld6_a1_a_rule_that_does_not_allow_deletion_deletes_nothing(ld6, monkeypatch):
    _ruled({"live_days": 30, "archive": "site"})
    _past_due(monkeypatch, days=365 * 50)
    with pytest.raises(AttestRefused, match="does not allow deletion"):
        retention.preview(SITE, LOG, archive_id=ld6["archive_id"])
    assert as_(ld6["client"], OP1).get(f"/api/v1/attest/logbooks/{LOG}").json()["retention"] == {
        "live_days": 30, "archive": "site"}


def test_ld6_retention_is_enforced_not_reported_as_later(tmp_path):
    from axiom.extensions.builtins.attest.logbooks import load_logbook

    from .test_w1 import KEPT_LOG

    kept = tmp_path / "kept_log.toml"
    kept.write_text(KEPT_LOG)
    book = load_logbook(kept)
    assert book.retention == {"live_days": 730, "archive": "site"}
    assert "logbook.retention" not in book.not_yet_enforced
    assert book.retention_rule is not None and book.retention_rule.deletes is False


# -- LD-6-A2: a tombstone is a person's signed record ------------------------------------------


@pytest.mark.acceptance("LD-6-A2")
def test_ld6_a2_a_tombstone_is_a_signed_record_of_its_own_kind_by_the_declared_role(ld6, monkeypatch):
    _past_due(monkeypatch)
    t = _tombstone(ld6).record
    assert t["kind"] == "tombstone" and t["meaning"] == "performed"
    assert t["target"] == ld6["seal"]["attestation_id"]
    assert t["signer"]["kind"] == "human" and t["signer"]["authority"]["roles"] == [CUSTODIAN]
    body = t["content"]["tombstone"]
    assert body["seal"]["attestation_id"] == ld6["seal"]["attestation_id"]
    assert body["covers"] == {"from_seq": 1, "to_seq": 2}
    assert body["rule"]["delete_after_days"] == 365 and body["rule"]["class"] == "system"
    assert [e["seq"] for e in body["erased"]] == [1, 2]
    assert body["archive"]["package_digest"] == ld6["package_digest"]
    assert body["labels"] == {"classification": "internal", "export_controlled": False}
    # Nothing it names is content: no title, no field value, no note.
    words = json.dumps(t["content"])
    assert SECRET_NOTE not in words and "the second covered entry" not in words


@pytest.mark.acceptance("LD-6-A2")
def test_ld6_a2_an_administration_role_cannot_sign_or_be_declared_for_a_tombstone(ld6, monkeypatch):
    _past_due(monkeypatch)
    with pytest.raises(AttestRefused, match="holds no role"):
        _tombstone(ld6, who=custodian(roles=("admin", "operator")))
    with pytest.raises(LogbookError, match="ADR-142 rule 8"):
        parse_retention({**RULE, "tombstone_by": ["admin"]}, LOG)


@pytest.mark.acceptance("LD-6-A2")
def test_ld6_a2_software_may_only_propose_a_tombstone(ld6, monkeypatch):
    _past_due(monkeypatch)
    head = max(_chain_rows())
    c = SkillContext(registry=bind_default(), state_dir=ld6["state"], logger=logging.getLogger("ld6"),
                     principal=PrincipalContext(handle=ME, posture="sso", assured=True),
                     actor="@clerk:site-a", surface="mcp")  # fmt: skip
    got = c.registry.invoke("attest.tombstone_propose", {"site": SITE, "logbook": LOG,
                            "archive_id": ld6["archive_id"], "for_principal": ME}, c)  # fmt: skip
    assert got.ok, got.errors
    assert max(_chain_rows()) == head  # proposed, never signed
    draft = service.draft(got.value["draft_id"])
    assert draft["kind"] == "tombstone" and draft["origin"] == "agent:clerk"
    assert tuple(c.registry.spec("attest.tombstone").surfaces) == ("cli",)  # a person, at the prompt
    assert tuple(c.registry.spec("attest.retention_erase").surfaces) == ("cli",)


# -- LD-6-A3: the chain verifies; covered records keep digest, signatures, labels -------------


@pytest.mark.acceptance("LD-6-A3")
def test_ld6_a3_after_a_tombstone_the_chain_verifies_end_to_end(ld6, monkeypatch):
    from axiom.attest import verify as attest_verify

    before = _chain_rows()
    t = _erased(ld6, monkeypatch)
    after = _chain_rows()
    for seq in (1, 2):
        old, new = before[seq], after[seq]
        assert "content" not in new and "evidence" not in new
        assert new["erased"] == {"tombstone": t["attestation_id"]}
        for k in ("digest", "prev_digest", "node_sig", "attestation_id", "signer", "kind", "meaning"):
            assert new[k] == old[k]
    assert after[3] == before[3] and after[4] == before[4]  # the seal and later records untouched
    report = service.verify_logbook(SITE, LOG, _keys(ld6))
    assert report.ok and report.checked == len(after), report
    assert attest_verify.chain(SITE, LOG, public_keys=_keys(ld6)).ok
    # A slice that stops before the tombstone still verifies: the tombstone is looked up.
    assert attest_verify.chain(SITE, LOG, 1, 2, public_keys=_keys(ld6)).ok
    # The labels stay: the door serves the record's labels and the tombstone carries them.
    got = _skill(ld6, "attest.show", {"site": SITE, "logbook": LOG, "attestation_id": ld6["rec"]["attestation_id"]})
    assert got.ok and SECRET_NOTE not in json.dumps(got.value, default=str)


@pytest.mark.acceptance("LD-6-A3")
def test_ld6_a3_a_content_less_record_no_tombstone_covers_fails_verification(ld6):
    rows = [dict(r) for _, r in sorted(_chain_rows().items())]
    stripped = {k: v for k, v in rows[0].items() if k not in ("content", "evidence")}
    stripped["erased"] = {"tombstone": "00000000-0000-0000-0000-000000000000"}
    report = verify_chain([stripped, *rows[1:]], _keys(ld6))
    assert not report.ok and report.first_bad_seq == 1 and report.reason == "content_removed_uncovered"


@pytest.mark.acceptance("LD-6-A3")
def test_ld6_a3_the_standalone_verifier_accepts_a_covered_record_and_no_other(ld6, monkeypatch):
    """The stdlib verifier an export or a package carries applies the same rule, with no Axiom."""
    import importlib.util

    from axiom.attest.tools import verify as tool

    spec = importlib.util.spec_from_file_location("ld6_standalone", tool.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _erased(ld6, monkeypatch)
    rows = [r for _, r in sorted(_chain_rows().items())]
    keys = _keys(ld6)
    assert mod.verify_records(rows, keys)[0] is True
    # Without the tombstone in the walk, the content-less records are not accepted.
    ok, *_rest, bad, reason = mod.verify_records([r for r in rows if r["kind"] != "tombstone"][:3], keys)
    assert (ok, bad, reason) == (False, 1, "content_removed_uncovered")


@pytest.mark.acceptance("LD-6-A3")
def test_ld6_a3_blobs_go_unless_a_live_record_names_the_same_hash(ld6, monkeypatch):
    blobs = attachments.blobs()
    assert blobs.exists(attachments.blob_key(SITE, ld6["second"]))
    t = _erased(ld6, monkeypatch)
    assert not blobs.exists(attachments.blob_key(SITE, ld6["second"]))
    assert blobs.exists(attachments.blob_key(SITE, ld6["photo"]))  # #4 still names it
    assert t["content"]["tombstone"]["blobs"] == {"removed": [ld6["second"]], "kept": [ld6["photo"]]}
    assert attachments.restore_findings(SITE) == []
    # The removed hash resolves to the tombstone at the attachment door, after the same check.
    rec2 = ld6["rec2"]
    url = f"/api/v1/attest/{LOG}/records/{rec2['attestation_id']}/attachments/{ld6['second']}?site={SITE}"
    status, body = ld4._get(ld6["client"], OP1, url)
    assert status == 410 and body["tombstone"]["attestation_id"] == t["attestation_id"]
    # The archived package went with the content it held.
    assert not list(ld6["cold"].rglob("*.zip"))
    assert archives.get(ld6["archive_id"])["status"] == "tombstoned"


@pytest.mark.acceptance("LD-6-A3")
def test_ld6_a3_every_copy_of_the_content_goes_presentations_drafts_and_answers(ld6, monkeypatch):
    _erased(ld6, monkeypatch)
    with store.session_scope() as s:
        shown = " ".join(str(x) for x in s.execute(text(
            "SELECT p.rendered, p.statement::text FROM attest_presentations p JOIN attest_drafts d "
            "ON d.draft_id = p.draft_id WHERE d.attestation_id = :a"),
            {"a": ld6["rec"]["attestation_id"]}).all())  # fmt: skip
        drafts = s.execute(text("SELECT content::text FROM attest_drafts WHERE attestation_id = :a"),
                           {"a": ld6["rec"]["attestation_id"]}).scalar()  # fmt: skip
    assert SECRET_NOTE not in shown and SECRET_NOTE not in drafts


@pytest.mark.acceptance("LD-6-A3")
def test_ld6_refuses_deletion_as_an_edit_or_a_row_delete(ld6):
    """The database guard: only the erasure a tombstone names is an allowed change."""
    for stmt in ("UPDATE attest_records SET record = record - 'content' WHERE seq = 1 AND logbook = :l",
                 "DELETE FROM attest_records WHERE seq = 1 AND logbook = :l",
                 "UPDATE attest_records SET record = jsonb_set(record, '{erased}', '{\"tombstone\": \"x\"}') "
                 "- 'content' - 'evidence' WHERE seq = 1 AND logbook = :l"):
        with store.session_scope() as s, pytest.raises(Exception, match="append-only"):
            s.execute(text(stmt), {"l": LOG})
            s.commit()


# -- LD-6-A4: index rows and fragments go; recall resolves to the tombstone ----------------------


@pytest.mark.acceptance("LD-6-A4")
def test_ld6_a4_index_rows_for_the_covered_records_are_removed_and_stay_removed(ld6, monkeypatch):
    ids = (ld6["rec"]["attestation_id"], ld6["rec2"]["attestation_id"])

    def indexed():
        with store.session_scope() as s:
            idx = f'{gold.ident(gold.schema())}."log_record_index"'
            n = s.execute(text(f"SELECT count(*) FROM {idx} WHERE attestation_id = ANY(:ids)"),  # nosemgrep
                          {"ids": list(ids)}).scalar()  # fmt: skip
            m = s.execute(text("SELECT count(*) FROM attest_attachment_index WHERE attestation_id = ANY(:ids)"),
                          {"ids": list(ids)}).scalar()  # fmt: skip
            f = s.execute(text("SELECT count(*) FROM attest_field_values WHERE attestation_id = ANY(:ids) "
                               "OR source_attestation_id = ANY(:ids)"), {"ids": list(ids)}).scalar()  # fmt: skip
        return n, m, f

    # A retired period keeps only its seal's summary (unit LD-8); rehydrating it indexes the
    # covered records again, warm, so the erasure has rows of theirs to remove.
    warmed = _skill(ld6, "attest.rehydrate", {"site": SITE, "logbook": LOG, "archive_id": ld6["archive_id"]})
    assert warmed.ok, warmed.errors
    assert indexed()[0] > 0
    _erased(ld6, monkeypatch)
    assert indexed() == (0, 0, 0)
    index.refresh([(SITE, LOG)], state_dir=ld6["tmp"])
    index.rebuild(state_dir=ld6["tmp"])  # a rebuild does not bring them back
    assert indexed()[0] == 0
    got = _skill(ld6, "attest.search", {"query": "covered entry", "site": SITE, "logbook": LOG})
    assert got.ok and not set(ld2_ids(got.value)) & set(ids)


@pytest.mark.acceptance("LD-6-A4")
def test_ld6_a4_a_recall_resolves_to_the_tombstone_and_fragments_are_retired(ld6, monkeypatch):
    from axiom.memory import references

    from . import test_ld3 as ld3

    mem_dir = ld6["tmp"] / "memory"
    mem_dir.mkdir()
    svc = ld3._memory(mem_dir, ME)
    w3 = {**ld6, "memory": svc}
    summary = "the morning round flagged a seal to watch"
    frag = ld3.write(w3, summary)
    retired: list[tuple[str, ...]] = []
    references.register_retirer("ld6-test", lambda pointers, tombstone: retired.append(tuple(pointers)) or
                                svc.composition.retire_references(pointers, tombstone=tombstone))  # fmt: skip
    try:
        t = _erased(ld6, monkeypatch)
    finally:
        references.register_retirer("ld6-test", None)
    pointer = references.pointer("attest", ld6["rec"]["digest"])
    assert retired and pointer in retired[0]
    got = log_refs.LogResolver().resolve(frag.content["reference"], caller=ld3.me(ld6["state"]),
                                         transport="mcp")  # fmt: skip
    assert got.allowed and got.tombstoned and got.record is None
    assert got.tombstone["removed_by"] == {"attestation_id": t["attestation_id"], "seq": t["seq"]}
    assert got.tombstone["digest"] == ld6["rec"]["digest"]
    # The fragment is out of recall: nothing derived from the removed content is served.
    result = svc.serve_fragments([frag], consumer=ld3.consumer(), query="seal",
                                 reference_caller=ld3.me(ld6["state"]))  # fmt: skip
    assert summary not in ld3.served_text(result, svc)
    assert not svc.composition.artifact_registry.find_by_name("fragment", frag.id)


@pytest.mark.acceptance("LD-6-A4")
def test_ld6_a4_the_journal_keeps_the_reference_and_loses_the_words(ld6, journal_db, monkeypatch):  # noqa: F811
    from axiom.extensions.builtins.journal import rows, writer

    writer.forget_partitions()  # this test's schema has none yet, whatever another test made
    writer.append(rows.from_brief(service.brief(ld6["rec"])))
    _erased(ld6, monkeypatch)
    from axiom.extensions.builtins.journal import store as jstore

    with jstore.session_scope() as s:
        got = s.execute(text("SELECT summary, ref FROM journal_events WHERE source_id = :a"),
                        {"a": ld6["rec"]["attestation_id"]}).one()  # fmt: skip
    assert "Round check" not in got.summary and got.ref["digest"] == ld6["rec"]["digest"]


# -- LD-6-A5: journal partitions drop only past the lane's floor -------------------------------


def _journal_row(lane, subject, at, n=0):
    from axiom.extensions.builtins.journal import rows, writer

    writer.forget_partitions()  # this test's schema has none yet, whatever another test made

    writer.append(rows.Row(lane=lane, subject=subject, source="ld6", source_id=f"{lane}-{at:%Y%m}-{n}", at=at,
                           status="ok", summary=f"{lane} event"))  # fmt: skip


@pytest.mark.acceptance("LD-6-A5")
def test_ld6_a5_journal_partitions_drop_only_at_or_past_the_lanes_floor(journal_db, tmp_path, monkeypatch):  # noqa: F811
    from axiom.extensions.builtins.journal import lanes, prune

    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    now = datetime(2026, 11, 20, tzinfo=UTC)
    _journal_row("platform", "platform.tick", datetime(2026, 8, 3, tzinfo=UTC))  # month floor, past it
    _journal_row("platform", "platform.tick", datetime(2026, 11, 2, tzinfo=UTC))  # this month
    _journal_row("tool_audit", "tool.call", datetime(2026, 7, 9, tzinfo=UTC))  # year floor, not past
    _journal_row("platform", "platform.tick", datetime(2026, 7, 10, tzinfo=UTC), n=1)
    plan = prune.preview(now=now)
    by = {p["month"]: p for p in plan["partitions"]}
    assert by["2026-08"]["droppable"] is True
    assert by["2026-07"]["droppable"] is False and "tool_audit" in by["2026-07"]["kept_for"]
    assert by["2026-11"]["droppable"] is False
    sid = prune.commit(owner=ME, now=now)["switch_id"]
    with pytest.raises(SwitchRefused, match="approver"):
        prune.drop(sid, approver="@system", override_soak="test: no soak")
    done = prune.drop(sid, approver=ME, override_soak="test: no soak")
    assert done["dropped"] == ["journal_events_p202608"]
    assert lanes.retention("platform")["effective_days"] == 30


@pytest.mark.acceptance("LD-6-A5")
def test_ld6_a5_a_site_setting_below_the_floor_is_reported_as_the_floor_and_never_applied(
        journal_db, monkeypatch):  # noqa: F811
    from axiom.extensions.builtins.journal import lanes, prune

    monkeypatch.setattr(lanes, "_requested_days", lambda lane: 5 if lane == "platform" else None)
    got = lanes.retention("platform")
    assert got == {"class": "month", "floor_days": 30, "effective_days": 30, "requested_days": 5,
                   "applied": False}  # fmt: skip
    now = datetime(2026, 11, 20, tzinfo=UTC)
    _journal_row("platform", "platform.tick", datetime(2026, 10, 5, tzinfo=UTC))  # ended 20 days ago
    by = {p["month"]: p for p in prune.preview(now=now)["partitions"]}
    assert by["2026-10"]["droppable"] is False


@pytest.mark.acceptance("LD-6-A5")
def test_ld6_shortening_a_floor_is_refused(ld6, monkeypatch):
    with pytest.raises(LogbookError, match="floor"):
        parse_retention({**RULE, "delete_after_days": 30}, LOG)  # system's floor is a year
    with pytest.raises(LogbookError, match="lifetime"):
        parse_retention({**RULE, "class": "lifetime"}, LOG)
    # A site may lengthen the age a log is deleted at, never shorten it: the floor is reported.
    site_settings.register(lambda site: {"retention": {LOG: {"delete_after_days": 10}}})
    try:
        eff = retention.report(SITE, LOG)["effective"]
        assert eff["delete_after_days"] == 365 and eff["site_setting_days"] == 10
        assert eff["site_setting_applied"] is False
    finally:
        site_settings.unregister()


# -- LD-6-A6: the retention report at the CLI and the Log page's door --------------------------


@pytest.mark.acceptance("LD-6-A6")
def test_ld6_a6_the_cli_and_the_log_page_report_declared_effective_and_next_due(ld6):
    got = _skill(ld6, "attest.retention", {"site": SITE, "logbook": LOG})
    assert got.ok, got.errors
    v = got.value
    assert v["declared"] == RULE
    assert v["effective"]["deletes"] is True and v["effective"]["delete_after_days"] == 365
    assert v["next_due"]["delete"]["archive_id"] == ld6["archive_id"]
    sealed_at = datetime.fromisoformat(v["periods"][0]["sealed_at"].replace("Z", "+00:00"))
    due = datetime.fromisoformat(v["next_due"]["delete"]["due_at"].replace("Z", "+00:00"))
    assert (due - sealed_at).days == 365
    body = as_(ld6["client"], OP1).get(f"/api/v1/attest/{LOG}/retention?site={SITE}")
    assert body.status_code == 200 and body.json()["effective"] == v["effective"]
    from axiom.extensions.builtins.attest.log_cli import LOG_SKILLS

    assert LOG_SKILLS["retention"] == "attest.retention"
    reads = [r for r in audit_trail.read_chain(audit_trail.audit_path(ld6["state"]))
             if (r.get("access") or {}).get("door") in ("attest.retention", "attest.http.retention")]  # fmt: skip
    assert len(reads) == 2


# -- LD-6-A7: only a sealed and archived period ------------------------------------------------


@pytest.mark.acceptance("LD-6-A7")
def test_ld6_a7_a_tombstone_naming_a_period_not_sealed_and_archived_is_refused(
        world, restore_log, blob_store, node_signer, tmp_path, monkeypatch):
    _ruled()
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    from axiom.infra.storage import provider_from_config

    archives.set_target(provider_from_config({"base_dir": str(tmp_path / "cold")}))
    try:
        draft = service.create_seal_draft(site_id=SITE, logbook=LOG, boundary="range", from_seq=1, to_seq=1,
                                          summary="One round.", origin="human", for_principal=ME)  # fmt: skip
        _sign(draft, node_signer)
        _past_due(monkeypatch)
        built = archives.build(SITE, LOG, by=ME)  # packaged, not committed
        with pytest.raises(AttestRefused, match="sealed and archived"):
            service.create_tombstone_draft(site_id=SITE, logbook=LOG, archive_id=built["archive_id"],
                                           origin="human", for_principal=ME)  # fmt: skip
        with pytest.raises(AttestRefused, match="no archive"):
            retention.preview(SITE, LOG, archive_id="not-an-archive")
    finally:
        archives.set_target(None)
        archives._packages.clear()  # noqa: SLF001


# -- must refuse -------------------------------------------------------------------------------


def test_ld6_refuses_before_the_rule_is_due(ld6):
    with pytest.raises(AttestRefused, match="not due"):
        retention.preview(SITE, LOG, archive_id=ld6["archive_id"])


def test_ld6_a_legal_hold_blocks_the_tombstone_and_the_erasure(ld6, monkeypatch):
    _past_due(monkeypatch)
    t = _tombstone(ld6).record
    retention.commit(t["attestation_id"], owner=ME)
    site_settings.register(lambda site: {"retention": {LOG: {"hold": "litigation 2026-17"}}})
    try:
        with pytest.raises(Exception, match="hold"):
            retention.erase(t["attestation_id"], approver=ME, override_soak="test: no soak")
        with pytest.raises(AttestRefused, match="hold"):
            retention.preview(SITE, LOG, archive_id=ld6["archive_id"])
    finally:
        site_settings.unregister()
    assert all(r.get("content") for r in _chain_rows().values())


def test_ld6_never_deletes_on_a_policy_error(ld6, monkeypatch):
    _past_due(monkeypatch)

    def broken(site):
        raise ConnectionError("settings unreachable")

    site_settings.register(broken)
    try:
        with pytest.raises(AttestRefused, match="cannot tell"):
            retention.preview(SITE, LOG, archive_id=ld6["archive_id"])
    finally:
        site_settings.unregister()


def test_ld6_the_erasure_is_a_named_persons_approved_act_and_never_silent(ld6, monkeypatch):
    _past_due(monkeypatch)
    t = _tombstone(ld6).record
    with pytest.raises(AttestRefused, match="commit"):
        retention.erase(t["attestation_id"], approver=ME, override_soak="test: no soak")
    retention.commit(t["attestation_id"], owner=ME)
    # Committed: reads hide the content already, and nothing is destroyed yet.
    assert all(r.get("content") for r in _chain_rows().values())
    shown = service.record(ld6["rec"]["attestation_id"])
    assert "content" not in shown
    with pytest.raises(SwitchRefused, match="approver"):
        retention.erase(t["attestation_id"], approver="@system", override_soak="test: no soak")
    with pytest.raises(AttestRefused, match="role"):
        retention.erase(t["attestation_id"], approver="@op2:site-a", override_soak="test: no soak")
    retention.erase(t["attestation_id"], approver=ME, override_soak="test: no soak")
    # The deletion is on the action chain, written before anything was destroyed; the tombstone
    # names everything deleted, and its row links the chain's record.
    deletions = [r for r in audit_trail.read_chain(audit_trail.audit_path(ld6["tmp"]))
                 if r.get("skill") == "retention.delete.attest.tombstone"]  # fmt: skip
    link = retention.get(t["attestation_id"])["deletion"]
    assert [d["hash"] for d in deletions] == [link["hash"]]
    assert deletions[0]["principal"] == ME and deletions[0]["outcome"] == "deleted-forever"


def test_ld6_a_preview_names_everything_and_deletes_nothing(ld6, monkeypatch):
    _past_due(monkeypatch)
    before = _chain_rows()
    got = _skill(ld6, "attest.tombstone_preview", {"site": SITE, "logbook": LOG,
                                                   "archive_id": ld6["archive_id"]})  # fmt: skip
    assert got.ok, got.errors
    v = got.value
    assert [e["seq"] for e in v["erased"]] == [1, 2]
    assert v["blobs"] == {"removed": [ld6["second"]], "kept": [ld6["photo"]]}
    assert v["package"]["package_digest"] == ld6["package_digest"]
    assert _chain_rows() == before
