# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-3: memory reference fragments for records (#1760, ADR-251 D2).

A memory fragment about a signed record is a **reference**: the record's pointer
(``axiom://attest/sha256:<digest>`` with its id, log and sequence), a short summary, provenance
(the ``(T, U, A, R)`` tuple and a ``SourceOrigin``) and the record's labels copied at write time.
It holds none of the record's content. Recall re-resolves the pointer through the log's door
(``attest.reference``) with the caller's rights at that moment, reading the gold view: a denied
pointer hides the summary, and an allowed one returns the record as of the gold watermark with a
deep link and one read record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import doors as log_doors  # noqa: E402
from axiom.extensions.builtins.attest import gold, registry, roles, service  # noqa: E402
from axiom.extensions.builtins.attest import references as log_refs  # noqa: E402
from axiom.extensions.builtins.attest.skills import DOOR_SKILLS, bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import _sign  # noqa: E402
from axiom.extensions.builtins.journal import rights  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.governance.doors import Reason  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.memory import references  # noqa: E402
from axiom.memory.serving import ConsumerCoordinate, DenyReason  # noqa: E402

SITE = "site-a"
LOG = "demo_log"
OPEN_LOG = "ld3_public_log"
ME = "@op1:site-a"
OTHER = "@op2:site-a"
AGENT = "axi"
NOTE = "pump seal weeping at the gland"
READING = "4.2"
DOOR = "attest.reference"


def _embed(texts):
    return [[b / 255.0 for b in hashlib.sha256(t.lower().encode()).digest()[:8]] for t in texts]


def _draft(logbook: str = LOG, title: str = "Pump seal noted", by: str = ME):
    return service.create_draft(
        site_id=SITE, logbook=logbook, entry_type="ROUND_CHECK", meaning="performed",
        content={"title": title, "fields": {"reading": READING, "walkdown": True, "note": NOTE}},
        origin="human", for_principal=by,
    )  # fmt: skip


def _memory(tmp: Path, *principals: str):
    """The cross-mem stack: one write path, the recall index, the serving gate."""
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs, add_user_agent_edge
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.recall import RecallIndex
    from axiom.memory.serving import ServingGate
    from axiom.memory.serving_service import MemoryServingService
    from axiom.memory.trust import TrustGraph
    from axiom.rag.sqlite_store import SQLiteRAGStore
    from axiom.vega.identity.keypair import generate_keypair

    kp = generate_keypair()
    graphs = AccessGraphs()
    for p in principals:
        graphs = add_user_agent_edge(graphs, p, AGENT)
    rag = SQLiteRAGStore(f"sqlite:///{tmp}/recall.db")
    rag.connect()
    composition = CompositionService(
        artifact_registry=ArtifactRegistry(backend=SQLiteBackend(tmp / "fragments.db")),
        audit_log=AuditLog(tmp / "memory-audit.jsonl", signing_keypair=kp),
        signing_keypair=kp,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=graphs,
        trust_graph=TrustGraph(),
        recall_index=RecallIndex(store=rag, embedder=_embed),
    )
    return MemoryServingService(composition=composition, gate=ServingGate())


@pytest.fixture
def world(client, attest_db, node_signer, tmp_path, monkeypatch):
    from axiom.extensions.builtins.attest import signing

    audit_trail._reset_for_tests()
    rights.forget_decisions()
    signing.set_provider(lambda: node_signer)
    monkeypatch.setenv("AXIOM_SITE", SITE)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    import axiom.infra.paths as paths

    monkeypatch.setattr(paths, "get_user_state_dir", lambda: tmp_path)
    book = registry.get(LOG)
    registry.register(replace(book, id=OPEN_LOG, handle=None, classification="public"))
    rec = _sign(_draft(), node_signer).record
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    yield {"rec": rec, "state": tmp_path, "signer": node_signer,
           "memory": _memory(mem_dir, ME, OTHER), "book": book}  # fmt: skip
    registry.register(book)
    gold.drop_for_tests()
    audit_trail._reset_for_tests()


def me(state: Path, handle: str = ME, **kw) -> log_doors.LogCaller:
    return log_doors.LogCaller(handle=handle, sites=frozenset({SITE}), state_dir=state, **kw)


def consumer(principal: str = ME, **kw) -> ConsumerCoordinate:
    return ConsumerCoordinate(principal=principal, harness="claude-code", account=principal, **kw)


def write(w, summary: str = "the morning round flagged a seal to watch", *, record=None,
          principal: str = ME, caller=None):  # fmt: skip
    rec = record or w["rec"]
    return references.write_reference(
        w["memory"].composition, record=rec["attestation_id"], summary=summary,
        principal_id=principal, agents={AGENT},
        caller=caller or me(w["state"], principal),
    )  # fmt: skip


def stored(w, fragment_id: str) -> dict:
    rows = w["memory"].composition.artifact_registry.find_by_name("fragment", fragment_id)
    assert len(rows) == 1
    return rows[0].data


def door_records(state: Path) -> list[dict]:
    return [r for r in audit_trail.read_chain(audit_trail.audit_path(state))
            if (r.get("access") or {}).get("door") == DOOR]  # fmt: skip


def served_text(result, svc) -> str:
    return svc.to_plaintext_block(result) + json.dumps(svc.to_mcp_payload(result), default=str)


# -- LD-3-A1: the stored row is a pointer, a summary, provenance and labels -------------------


@pytest.mark.acceptance("LD-3-A1")
def test_ld3_a1_the_stored_row_holds_pointer_summary_provenance_labels_and_no_content(world):
    rec = world["rec"]
    frag = write(world)
    data = stored(world, frag.id)
    content = data["content"]
    pointer = f"axiom://attest/sha256:{rec['digest']}"
    assert set(content) == {"ref", "reference", "summary", "labels"}
    assert content["ref"] == pointer
    assert content["reference"] == {"scheme": "attest", "id": rec["attestation_id"], "scope": LOG,
                                    "seq": rec["seq"], "site": SITE, "digest": rec["digest"]}  # fmt: skip
    assert content["labels"] == {"classification": "internal", "export_controlled": False}
    assert data["cognitive_type"] == "resource"
    # Provenance: the (T, U, A, R) tuple and the SourceOrigin.
    prov = data["provenance"]
    assert prov["principal_id"] == ME and prov["agents"] == [AGENT] and prov["timestamp"]
    assert prov["resources"] == [pointer]
    assert prov["origin"]["source_ref"] == pointer and prov["origin"]["account"] == ME
    assert prov["origin"]["harness"] == "axiom:attest"
    # None of the record's content fields or observed values, in any column of the row.
    blob = json.dumps(data, default=str)
    for value in (NOTE, READING, "walkdown", "Pump seal noted"):
        assert not re.search(rf"(?<![\w.]){re.escape(value)}(?![\w])", blob), value
    # The rag-memory chunk carries the pointer and the summary only.
    chunk = world["memory"].composition.recall_index  # noqa: F841 - the projection is below
    from axiom.memory.recall_projection import fragment_to_recall_chunk

    assert fragment_to_recall_chunk(frag).chunk_text == (
        f"ref: {pointer}\nsummary: the morning round flagged a seal to watch"
    )


@pytest.mark.acceptance("LD-3-A1")
def test_ld3_a1_must_refuse_a_copy_of_record_content_a_draft_a_tray_item_and_no_labels(
    world, monkeypatch
):
    with pytest.raises(references.ReferenceRefused, match="content"):
        write(world, f"note says {NOTE}")
    with pytest.raises(references.ReferenceRefused, match="content"):
        write(world, f"reading was {READING} today")
    with pytest.raises(references.ReferenceRefused, match="content"):
        write(world, "title: Pump seal noted")
    # A draft (or a draft software put in a person's tray) is not a signed record: not in gold.
    draft_id = _draft()
    with pytest.raises(references.ReferenceRefused, match="signed record"):
        references.write_reference(world["memory"].composition, record=str(draft_id),
                                   summary="a draft", principal_id=ME, agents={AGENT},
                                   caller=me(world["state"]))  # fmt: skip
    # A record whose log declares no labels: refused, never written unlabeled.
    monkeypatch.delitem(registry._logbooks, LOG)
    with pytest.raises(references.ReferenceRefused, match="labels"):
        write(world)
    # The one write path refuses a reference shape with no labels, or with any extra field.
    comp = world["memory"].composition
    base = {"ref": f"axiom://attest/sha256:{world['rec']['digest']}", "summary": "x",
            "reference": {"scheme": "attest", "id": world["rec"]["attestation_id"], "scope": LOG,
                          "seq": world["rec"]["seq"], "site": SITE,
                          "digest": world["rec"]["digest"]}}  # fmt: skip
    with pytest.raises(ValueError, match="labels"):
        comp.write(content=base, cognitive_type="resource", principal_id=ME, agents={AGENT},
                   resources=set())  # fmt: skip
    with pytest.raises(ValueError, match="fields"):
        comp.write(content={**base, "labels": {"classification": "internal",
                                               "export_controlled": False}, "content": {"note": NOTE}},
                   cognitive_type="resource", principal_id=ME, agents={AGENT}, resources=set())  # fmt: skip
    assert comp.artifact_registry.list(kind="fragment") == []


@pytest.mark.acceptance("LD-3-A1")
def test_ld3_a1_a_writer_who_may_not_read_the_record_cannot_reference_it(world):
    outsider = log_doors.LogCaller(handle="@op9:site-b", sites=frozenset({"site-b"}),
                                   state_dir=world["state"])  # fmt: skip
    with pytest.raises(references.ReferenceRefused) as exc:
        write(world, principal="@op9:site-b", caller=outsider)
    assert exc.value.code == g.WIRE[Reason.SITE][1]
    assert world["memory"].composition.artifact_registry.list(kind="fragment") == []


# -- LD-3-A2: labels at write time; a relabel is seen through the pointer --------------------


@pytest.mark.acceptance("LD-3-A2")
def test_ld3_a2_labels_copied_at_write_and_a_relabel_is_seen_at_recall_not_by_editing(world):
    frag = write(world)
    assert stored(world, frag.id)["content"]["labels"] == log_doors.labels_for(LOG)
    before = json.dumps(stored(world, frag.id), sort_keys=True)
    registry.register(replace(world["book"], classification="regulated"))
    svc = world["memory"]
    denied = svc.serve_fragments([frag], consumer=consumer(), query="seal",
                                 reference_caller=me(world["state"]))  # fmt: skip
    assert denied.items == [] and [d.code for d in denied.denials] == ["classification"]
    cleared = svc.serve_fragments([frag], consumer=consumer(), query="seal",
                                  reference_caller=me(world["state"], ceiling="regulated"))  # fmt: skip
    assert [i.fragment_id for i in cleared.items] == [frag.id]
    assert cleared.resolved[frag.id]["labels"]["classification"] == "regulated"
    # The fragment was never edited: its row is byte-identical and still carries the old labels.
    assert json.dumps(stored(world, frag.id), sort_keys=True) == before


# -- LD-3-A3: a revoked grant denies with the door's reason; the summary is not served ---------


@pytest.mark.acceptance("LD-3-A3")
def test_ld3_a3_after_a_revoke_recall_denies_with_the_doors_reason_and_hides_the_summary(world):
    registry.register(replace(world["book"], readers=("auditor",)))
    roles.grant(OTHER, SITE, "auditor", by=ME, state_dir=world["state"])
    summary = "the other operator wants to recheck the seal"
    frag = write(world, summary, principal=OTHER)
    roles.revoke(OTHER, SITE, "auditor", by=ME, state_dir=world["state"])
    svc = world["memory"]
    before = len(door_records(world["state"]))
    result = svc.serve("recheck the seal", consumer=consumer(OTHER),
                       reference_caller=me(world["state"], OTHER))  # fmt: skip
    assert result.items == []
    [denial] = [d for d in result.denials if d.fragment_id == frag.id]
    assert denial.reason is DenyReason.REFERENCE_DENIED
    assert denial.code == g.WIRE[Reason.REVOKED][1]  # the door's answer: not found
    assert summary not in served_text(result, svc)
    # The read record names the exact reason; the fragment still exists, unchanged.
    [rec] = door_records(world["state"])[before:]
    assert rec["outcome"] == "denied" and rec["access"]["reason"] == "revoked"
    assert rec["access"]["ids"] == [] and rec["principal"] == OTHER
    assert stored(world, frag.id)["content"]["summary"] == summary
    # The ungated ledger listings never show a reference's summary either.
    from axiom.extensions.builtins.memory import mcp_server

    shown = mcp_server._fragment_to_dict(frag)
    assert summary not in json.dumps(shown) and shown["ref"] == frag.content["ref"]


@pytest.mark.acceptance("LD-3-A3")
def test_ld3_a3_a_reference_with_no_caller_able_to_resolve_it_is_never_served(world, monkeypatch):
    frag = write(world)
    svc = world["memory"]
    monkeypatch.setattr(log_refs, "caller_for", lambda principal: log_doors.LogCaller(handle=None))
    result = svc.serve_fragments([frag], consumer=consumer(), query="seal")
    assert result.items == [] and result.denials[0].code == g.WIRE[Reason.UNRESOLVED_CALLER][1]
    # An unknown pointer scheme fails closed.
    odd = replace(frag, content={**frag.content, "reference": {**frag.content["reference"],
                                                               "scheme": "nowhere"}})  # fmt: skip
    result = svc.serve_fragments([odd], consumer=consumer(), query="seal",
                                 reference_caller=me(world["state"]))  # fmt: skip
    assert result.items == [] and result.denials[0].code == "policy_unavailable"


# -- LD-3-A4: as of the gold watermark, a deep link, one read record -------------------------


@pytest.mark.acceptance("LD-3-A4")
def test_ld3_a4_recall_returns_the_record_as_of_gold_with_a_deep_link_and_one_read_record(world):
    rec = world["rec"]
    frag = write(world)
    svc = world["memory"]
    before = len(door_records(world["state"]))
    result = svc.serve("morning round seal", consumer=consumer(),
                       reference_caller=me(world["state"]))  # fmt: skip
    assert [i.fragment_id for i in result.items] == [frag.id]
    got = result.resolved[frag.id]
    aid = rec["attestation_id"]
    assert got["as_of"] == gold.watermark(SITE, LOG) and got["as_of"]
    assert got["link"] == f"/api/v1/attest/{LOG}/records/{aid}?site={SITE}"
    assert got["url"] == f"#/log/{LOG}/{aid}"
    assert got["record"]["attestation_id"] == aid and got["record"]["digest"] == rec["digest"]
    assert got["record"]["content"]["fields"]["note"] == NOTE  # read through the door, from gold
    payload = svc.to_mcp_payload(result)
    assert payload["fragments"][0]["record"]["link"] == got["link"]
    [read] = door_records(world["state"])[before:]
    a = read["access"]
    assert read["outcome"] == "ok" and read["principal"] == ME
    assert (a["ids"], a["count"], a["door"], a["reason"]) == ([aid], 1, DOOR, None)
    assert a["labels"] == {"classification": "internal", "export_controlled": False}
    assert set(a) <= set(g.ACCESS_FIELDS) and NOTE not in json.dumps(read)


@pytest.mark.acceptance("LD-3-A4")
def test_ld3_a4_the_reference_door_is_a_skill_door_in_the_one_registry(world):
    door = g.get(DOOR)
    assert door.extension == "attest" and door.shape == "target" and door.skill == DOOR
    assert door.record_param == "attestation_id" and DOOR in DOOR_SKILLS
    assert DOOR in gold.COMMENT  # the gold view names every door that serves it
    ctx = SkillContext(registry=bind_default(), state_dir=world["state"],
                       logger=logging.getLogger("ld3"), actor=ME, surface="chat",
                       principal=PrincipalContext(handle=ME, posture="sso", assured=True))  # fmt: skip
    got = invoke_capability(ctx.registry, DOOR, {"attestation_id": world["rec"]["attestation_id"]},
                            ctx, surface="agent_tool")  # fmt: skip
    assert got.ok, got.errors
    assert got.value["as_of"] == gold.watermark(SITE, LOG)
    assert got.value["record"]["attestation_id"] == world["rec"]["attestation_id"]


# -- LD-3-A5: sync and write-back carry the pointer and summary, only where labels allow -------


@pytest.mark.acceptance("LD-3-A5")
def test_ld3_a5_write_back_emits_pointer_and_summary_only_and_nothing_above_the_target(
    world, tmp_path
):
    from axiom.memory.rendering import SESSION_BOUNDARY, InstructionFileWriteBack
    from axiom.memory.sync.engine import SyncEngine

    public = _sign(_draft(OPEN_LOG, "Public bulletin"), world["signer"]).record
    open_ref = write(world, "an open notice for everyone about the round", record=public)
    internal_ref = write(world, "an internal note to recheck the seal")
    engine = SyncEngine(composition=world["memory"].composition, principal=ME,
                        account_set=frozenset({ME}))  # fmt: skip
    target = InstructionFileWriteBack(path=tmp_path / "repo" / "AGENTS.md")
    out = engine.propagate_to(consumer(), targets=[target], cadence=SESSION_BOUNDARY,
                              session_id="s1", epoch=1)  # fmt: skip
    written = target.path.read_text()
    assert f"ref: {open_ref.content['ref']}" in written
    assert "summary: an open notice for everyone about the round" in written
    # A target declares no ceiling: only a public record's pointer may leave.
    assert internal_ref.content["ref"] not in written and "recheck the seal" not in written
    assert {d.fragment_id: d.reason for d in out.denials}[internal_ref.id] is DenyReason.LABEL_CEILING
    # Never the labels' internals, never the record's content.
    for leak in (NOTE, READING, "Public bulletin", "walkdown", '"classification"'):
        assert leak not in written, leak
    # A target cleared for internal receives both; still pointer and summary only.
    target2 = InstructionFileWriteBack(path=tmp_path / "repo2" / "AGENTS.md")
    engine.propagate_to(consumer(record_ceiling="internal"), targets=[target2],
                        cadence=SESSION_BOUNDARY, session_id="s1", epoch=1)  # fmt: skip
    both = target2.path.read_text()
    assert internal_ref.content["ref"] in both and NOTE not in both
    # A relabel upward after the write is honoured: the current labels decide, not the copy.
    registry.register(replace(registry.get(OPEN_LOG), classification="regulated"))
    target3 = InstructionFileWriteBack(path=tmp_path / "repo3" / "AGENTS.md")
    engine.propagate_to(consumer(record_ceiling="internal"), targets=[target3],
                        cadence=SESSION_BOUNDARY, session_id="s1", epoch=1)  # fmt: skip
    assert open_ref.content["ref"] not in target3.path.read_text()


# -- LD-3-A6: a tombstoned record resolves to its tombstone ----------------------------------


@pytest.mark.acceptance("LD-3-A6")
def test_ld3_a6_a_record_whose_content_is_gone_resolves_to_the_tombstone_never_to_a_copy(
    world, monkeypatch
):
    """LD-6 removes a covered record's content and keeps its digest, signatures and labels
    (LD-6-A3), so gold projects the row with no content. That is simulated here on the gold
    read itself: the real row, read from gold, with its content gone."""
    rec = world["rec"]
    summary = "the morning round flagged a seal to watch"
    frag = write(world, summary)
    real = log_refs._gold_row

    def content_removed(**kw):
        row = real(**kw)
        if row is not None and row["attestation_id"] == rec["attestation_id"]:
            row = {**row, "content": None}
        return row

    monkeypatch.setattr(log_refs, "_gold_row", content_removed)
    svc = world["memory"]
    result = svc.serve_fragments([frag], consumer=consumer(), query="seal",
                                 reference_caller=me(world["state"]))  # fmt: skip
    got = result.resolved[frag.id]
    assert got["tombstoned"] is True and got["record"] is None
    assert got["tombstone"] == {"attestation_id": rec["attestation_id"], "digest": rec["digest"],
                                "labels": {"classification": "internal", "export_controlled": False}}  # fmt: skip
    text_out = served_text(result, svc)
    assert NOTE not in text_out and READING not in text_out
    # Nothing derived from the removed content is served: not even the summary.
    assert summary not in text_out and "tombstone" in text_out


# -- the one registry: the new door is a row, never a second check -----------------------------


@pytest.mark.acceptance("LD-3-A3")
def test_ld3_the_reference_door_answers_every_reason_by_the_one_check(world, monkeypatch):
    """Every reason at the reference door gives the one answer (the LD-1 matrix drives the
    skill transports; this drives the memory path, which reaches the door outside a skill)."""
    from axiom.infra import site_authority

    frag = write(world)
    svc = world["memory"]
    cases = {
        Reason.UNRESOLVED_CALLER: log_doors.LogCaller(handle=None),
        Reason.SITE: log_doors.LogCaller(handle="@op9:site-b", sites=frozenset({"site-b"})),
        Reason.CLASSIFICATION: None,
        Reason.EXPORT_CONTROL: None,
    }
    for reason, caller in cases.items():
        if reason is Reason.CLASSIFICATION:
            registry.register(replace(world["book"], classification="regulated"))
        if reason is Reason.EXPORT_CONTROL:
            registry.register(replace(world["book"], export_controlled=True))
        got = svc.serve_fragments([frag], consumer=consumer(), query="x",
                                  reference_caller=caller or me(world["state"]))  # fmt: skip
        assert got.items == [] and got.denials[0].code == g.WIRE[reason][1], reason
        registry.register(world["book"])

    def unreachable(*_a, **_k):
        raise ConnectionError("GUARD is unreachable")

    monkeypatch.setattr(site_authority, "decide_site_read", unreachable)
    got = svc.serve_fragments([frag], consumer=consumer(), query="x", reference_caller=me(world["state"]))
    assert got.items == [] and got.denials[0].code == "policy_unavailable"
