# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-4: attachments by content hash through the attest door (#1761,
ADR-251 D6).

An attachment is a blob stored once by its hash through the storage-provider abstraction; the
signed entry names the hash, media type and size inside its signed bytes; the blob is served only
by the attest door that admits a record naming it; what is extracted from it is a labelled index.
The world is LD-1's (one signed record in the demo log at site-a), with a blob store of its own.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from axiom.attest.canonical import digest  # noqa: E402
from axiom.extensions.builtins.attest import attachments, registry, service  # noqa: E402
from axiom.extensions.builtins.attest.service import AttestRefused  # noqa: E402
from axiom.extensions.builtins.attest.skills import bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import _round_check, _sign  # noqa: E402
from axiom.governance import doors as g  # noqa: E402
from axiom.governance.doors import Reason  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.infra.storage import configured, provider_from_config  # noqa: E402

from . import test_ld1 as ld1  # noqa: E402
from .test_ld1 import LOG, ME, SITE, _app_routes, arrange  # noqa: E402

#: LD-1's world and its log restorer, used here as fixtures.
world = ld1.world
restore_log = ld1.restore_log

ROOT = Path(__file__).resolve().parents[2]
DOOR = "attest.http.attachment"
PHOTO = b"\x89PNG\r\n\x1a\n" + b"ld4 photo bytes nobody may read across a door " * 8
NOTES = ("# Inspection notes\n\n" + "The pump casing was inspected and found dry. " * 40
         + "\n\n## Follow up\n\n" + "Recheck the seal at the next round. " * 40).encode()  # fmt: skip
CLIP = b"\x00\x00\x00\x18ftypmp42 ld4 video rendition bytes " * 4
RENDITION = {"rendition": "720p-h264", "policy_version": "stream-loss/3",
             "loss": {"from_sha256": "a" * 64, "lossy": True, "kept": "720p, 15 fps"}}  # fmt: skip


@pytest.fixture
def blob_store(tmp_path):
    base = tmp_path / "blobs"
    attachments.set_store(provider_from_config({"base_dir": str(base)}))
    attachments.set_embedder(lambda texts, tier: ([[float(len(t)), 1.0] for t in texts], "test-embedder"))
    attachments.set_image_reader(lambda path, media: ("PUMP 7 NAMEPLATE", "test-ocr"))
    yield base
    attachments.set_store(None)
    attachments.set_embedder(None)
    attachments.set_image_reader(None)


def _keys(signer):
    return {signer.key_id: signer.public_bytes}


def _signed(signer, *files, **content):
    """A record carrying ``files`` (``(bytes, media_type, extra)``), attached before signing."""
    draft_id = _round_check(**content)
    for data, media, extra in files:
        attachments.attach(draft_id, data, media, by=ME, **extra)
    return _sign(draft_id, signer).record


@pytest.fixture
def att(world, blob_store, node_signer):
    """LD-1's world, plus a record in the same log carrying a photo."""
    rec = _signed(node_signer, (PHOTO, "image/png", {"name": "pump.png"}))
    sha = attachments.refs_of(rec)[0]["sha256"]
    return {**world, "att": rec, "sha": sha, "blobs": blob_store}


def _att_url(rec, sha, site=SITE):
    return f"/api/v1/attest/{rec['logbook']}/records/{rec['attestation_id']}/attachments/{sha}?site={site}"


def _get(c, user, url):
    c.cookies.clear()
    if user is not None:
        as_(c, user)
    r = c.get(url)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.content
    return r.status_code, body


def _ctx(state: Path, handle: str | None) -> SkillContext:
    principal = PrincipalContext(handle=handle, posture="sso", assured=True) if handle else None
    return SkillContext(registry=bind_default(), state_dir=state, logger=logging.getLogger("ld4"),
                        principal=principal, actor=handle or "", surface="cli")  # fmt: skip


# -- LD-4-A1: stored once, keyed by the hash ------------------------------------------------


@pytest.mark.acceptance("LD-4-A1")
def test_ld4_a1_the_same_bytes_twice_are_stored_once_under_their_hash(att, node_signer):
    c = att["client"]
    draft = as_(c, OP1).post("/api/v1/attest/drafts", json={
        "logbook": LOG, "entry_type": "ROUND_CHECK", "meaning": "performed", "title": "Round check",
        "fields": {"reading": "ok", "walkdown": True}, "site": SITE}).json()["draft_id"]  # fmt: skip
    url = f"/api/v1/attest/drafts/{draft}/attachments?name=pump.png"
    first = c.post(url, content=PHOTO, headers={"content-type": "image/png"})
    again = c.post(url, content=PHOTO, headers={"content-type": "image/png"})
    assert first.status_code == again.status_code == 201, first.text
    assert first.json() == again.json()
    assert service.draft(draft)["attachments"] == [first.json()["attachment"]]  # one entry, once
    rec = _sign(draft, node_signer).record
    # The same bytes on two entries (the fixture's and this one) are one blob, named by the hash.
    sha = attachments.sha256_of(PHOTO)
    assert att["sha"] == sha and attachments.refs_of(rec)[0]["sha256"] == sha
    assert attachments.list_blobs(SITE) == [{"sha256": sha, "bytes": len(PHOTO)}]
    assert attachments.blob_key(SITE, sha) == f"{SITE}/sha256/{sha}"
    stored = [p for p in att["blobs"].rglob("*") if p.is_file()]
    assert [p.relative_to(att["blobs"]).as_posix() for p in stored] == [f"{SITE}/sha256/{sha}"]


# -- LD-4-A2: the hash is inside the signed bytes; no byte of the blob is in the chain ------


@pytest.mark.acceptance("LD-4-A2")
def test_ld4_a2_a_changed_byte_fails_verification_and_the_chain_holds_no_bytes(att, node_signer):
    rec, sha = att["att"], att["sha"]
    assert attachments.refs_of(rec) == [{"sha256": sha, "media_type": "image/png", "bytes": len(PHOTO),
                                         "name": "pump.png"}]  # fmt: skip
    assert service.verify_logbook(SITE, LOG, _keys(node_signer)).ok
    # The hash is covered by the signature: a record naming another hash is not the one signed.
    forged = json.loads(json.dumps(rec))
    forged["evidence"]["attachments"][0]["sha256"] = "b" * 64
    assert digest(forged) != rec["digest"]
    # No byte of the attachment is in the chain row.
    row = json.dumps(service.record(rec["attestation_id"]))
    assert "ld4 photo bytes" not in row and PHOTO.hex()[:32] not in row
    # One changed byte of the blob fails the entry's verification, whole chain and slice.
    blob = att["blobs"] / attachments.blob_key(SITE, sha)
    blob.write_bytes(PHOTO[:-1] + b"!")
    report = service.verify_logbook(SITE, LOG, _keys(node_signer))
    assert (report.ok, report.first_bad_seq, report.reason) == (False, rec["seq"], "attachment_altered")
    from axiom.attest import verify as verify_lib

    sliced = verify_lib.chain(SITE, LOG, rec["seq"], rec["seq"], public_keys=_keys(node_signer))
    assert (sliced.ok, sliced.reason) == (False, "attachment_altered")
    # And the door never serves altered bytes.
    status, body = _get(att["client"], OP1, _att_url(rec, sha))
    assert (status, body["code"]) == (409, "attachment_altered")


# -- LD-4-A3: reachable only through the door, by a record naming it ------------------------


@pytest.mark.acceptance("LD-4-A3")
def test_ld4_a3_the_door_serves_the_bytes_of_a_record_naming_them_and_records_the_read(att):
    rec, sha = att["att"], att["sha"]
    c = att["client"]
    as_(c, OP1)
    r = c.get(_att_url(rec, sha), follow_redirects=False)
    assert r.status_code == 200 and r.content == PHOTO
    assert "location" not in r.headers  # bytes, never a pointer to where they are
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("attachment")
    chain = audit_trail.read_chain(audit_trail.audit_path(att["state"]))
    reads = [x for x in chain if (x.get("access") or {}).get("door") == DOOR]
    assert len(reads) == 1 and reads[0]["outcome"] == "ok"
    assert reads[0]["access"]["ids"] == [rec["attestation_id"]]
    assert reads[0]["access"]["labels"] == registry.get(LOG).labels


@pytest.mark.acceptance("LD-4-A3")
def test_ld4_a3_a_route_walk_finds_no_other_path_to_a_blob(monkeypatch):
    monkeypatch.setenv("AXIOM_JOURNAL_INGEST", "0")
    from axiom.extensions.builtins.library import api as library_api

    router = fastapi.APIRouter(prefix="/api/v1")
    try:
        library_api.register_routes(router)
    except Exception:  # noqa: BLE001 - the Library's own routes, when it can mount here
        pass
    routes = _app_routes() | {(m, r.path) for r in router.routes for m in getattr(r, "methods", ())}
    blobby = {(m, p) for m, p in routes if m == "GET" and ("attachment" in p or "blob" in p or "{sha256}" in p)}
    assert blobby == {("GET", g.get(DOOR).route[1])}
    # In code: only the door reads a blob, nothing makes a location for one, and nothing outside
    # the module touches the store.
    readers, urls = set(), set()
    for path in (ROOT / "src" / "axiom").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "attachments":
                if node.attr in ("read", "blobs", "_get"):
                    readers.add(rel)
            if (isinstance(node, ast.Attribute) and node.attr == "get_canonical_url"
                    and ("/attest/" in rel)):  # fmt: skip
                urls.add(rel)
    # The door serves a blob; the archive (unit LD-5) copies each blob a sealed period names into
    # its package, which is read back only through the log's doors. Nothing else reads the store.
    assert readers <= {"src/axiom/extensions/builtins/attest/api.py",
                       "src/axiom/extensions/builtins/attest/archives.py"}, readers
    assert urls == set(), urls


@pytest.mark.acceptance("LD-4-A3")
def test_ld4_a3_a_blob_no_record_names_is_never_in_the_record_prefix(world, blob_store, node_signer):
    draft_id = _round_check()
    ref = attachments.attach(draft_id, PHOTO, "image/png", by=ME)
    # Staged under the draft, not in the record prefix: nothing can be served from it.
    assert attachments.list_blobs(SITE) == []
    assert attachments.restore_findings(SITE) == []
    service.discard(draft_id, by=ME)
    assert [p for p in blob_store.rglob("*") if p.is_file()] == []
    # A blob put in the record prefix by hand is a finding: named by no record.
    stray = blob_store / attachments.blob_key(SITE, ref["sha256"])
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(PHOTO)
    assert [f.problem for f in attachments.restore_findings(SITE)] == ["blob_unnamed"]


@pytest.mark.acceptance("LD-4-A3")
def test_ld4_a3_listing_the_blob_prefix_is_an_operator_verb_that_writes_a_read_record(att):
    ctx = _ctx(att["state"], ME)
    result = invoke_capability(ctx.registry, "attest.blobs", {"site": SITE}, ctx, surface="cli")
    assert result.ok and result.value["blobs"] == [{"sha256": att["sha"], "bytes": len(PHOTO)}]
    assert "url" not in json.dumps(result.value)
    listed = [x for x in audit_trail.read_chain(audit_trail.audit_path(att["state"]))
              if x.get("skill") == "attest.blobs"]  # fmt: skip
    assert len(listed) == 1 and listed[0]["access"]["what"] == "blobs"
    assert listed[0]["access"]["count"] == 1 and listed[0]["principal"] == ME
    from axiom.extensions.builtins.attest.skills import SPECS

    spec = next(s for s in SPECS if s.name == "attest.blobs")
    assert tuple(spec.surfaces) == ("cli",)  # never an agent's or an MCP client's


# -- LD-4-A4: denied the record, denied its blobs, with the same reason ---------------------


@pytest.mark.acceptance("LD-4-A4")
@pytest.mark.parametrize("reason", list(Reason), ids=lambda r: r.value)
def test_ld4_a4_a_caller_denied_the_record_is_denied_its_blobs_with_the_same_reason(
    reason, att, restore_log, monkeypatch
):
    who = arrange(reason, monkeypatch, att["state"])
    rec, sha, c = att["att"], att["sha"], att["client"]
    record_url = f"/api/v1/attest/{LOG}/records/{rec['attestation_id']}?site={SITE}"
    blob = _get(c, who["user"], _att_url(rec, sha))
    record = _get(c, who["user"], record_url)
    assert blob == record and blob[0] == g.WIRE[reason][0], (blob, record)
    assert "ld4 photo" not in json.dumps(blob[1])


@pytest.mark.acceptance("LD-4-A4")
def test_ld4_a4_the_attachment_door_is_a_row_of_the_ld1_matrix():
    door = g.get(DOOR)
    assert door.record_param == "attestation_id" and door.shape == "target"
    assert {p.values[2] for p in ld1.ROWS if p.values[0].id == DOOR} == set(Reason)


@pytest.mark.acceptance("LD-4-A4")
def test_ld4_a4_a_known_hash_confirms_nothing_across_the_door(att, node_signer, restore_log, monkeypatch):
    """No hash oracle: whether a hash is stored never changes an answer the caller gets."""
    from axiom.extensions.builtins.attest import access_requests

    monkeypatch.setattr(access_requests, "knowable", lambda *_a, **_k: False)
    c, sha = att["client"], att["sha"]
    plain = att["rec"]  # readable, carries no attachment
    other = "c" * 64
    missing = {**plain, "attestation_id": "00000000-0000-0000-0000-000000000000"}
    answers = {_json(_get(c, OP1, _att_url(plain, sha))), _json(_get(c, OP1, _att_url(plain, other))),
               _json(_get(c, OP1, _att_url(missing, sha)))}  # fmt: skip
    assert len(answers) == 1 and next(iter(answers))[0] == 404
    # A reader denied the log: the stored hash and an unknown one answer alike.
    registry.register(replace(registry.get(LOG), readers=("auditor",)))
    denied = {_json(_get(c, OP2, _att_url(att["att"], sha))), _json(_get(c, OP2, _att_url(att["att"], other)))}
    assert len(denied) == 1 and next(iter(denied))[0] == 404


def _json(answer):
    return answer[0], json.dumps(answer[1], sort_keys=True) if not isinstance(answer[1], bytes) else answer[1]


# -- LD-4-A5: extraction rows carry the labels, the record and the hash ----------------------


@pytest.mark.acceptance("LD-4-A5")
def test_ld4_a5_extracted_rows_are_labelled_and_chunked_by_type(world, blob_store, node_signer):
    rec = _signed(node_signer, (NOTES, "text/markdown", {"name": "notes.md"}),
                  (PHOTO, "image/png", {"name": "pump.png"}), (CLIP, "video/mp4", dict(RENDITION)))  # fmt: skip
    rows = attachments.index_rows(rec["attestation_id"])
    labels = registry.get(LOG).labels
    by = {r["sha256"]: [] for r in rows}
    for r in rows:
        assert (r["site_id"], r["logbook"], r["attestation_id"]) == (SITE, LOG, rec["attestation_id"])
        assert {"classification": r["classification"], "export_controlled": r["export_controlled"]} == labels
        by[r["sha256"]].append(r)
    doc, img, vid = (by[attachments.sha256_of(b)] for b in (NOTES, PHOTO, CLIP))
    # A document: parsed, chunked and embedded.
    doc_text = [r for r in doc if r["part"] == "text"]
    assert len(doc_text) > 1 and all(r["embedding"] and r["embed_model"] == "test-embedder" for r in doc_text)
    assert "pump casing" in " ".join(r["text"] for r in doc_text) and {r["kind"] for r in doc} == {"document"}
    # An image: read (OCR or a caption), not embedded.
    img_text = [r for r in img if r["part"] == "text"]
    assert [r["text"] for r in img_text] == ["PUMP 7 NAMEPLATE"] and img_text[0]["embedding"] is None
    assert next(r for r in img if r["part"] == "metadata")["meta"]["read_by"] == "test-ocr"
    # A video: metadata only, naming its rendition and policy.
    assert [r["part"] for r in vid] == ["metadata"] and vid[0]["kind"] == "video"
    assert {k: vid[0]["meta"][k] for k in RENDITION} == RENDITION


@pytest.mark.acceptance("LD-4-A5")
def test_ld4_a5_an_extraction_row_without_labels_is_refused(world, blob_store, node_signer, restore_log, monkeypatch):
    rec = _signed(node_signer, (NOTES, "text/plain", {}))
    row = attachments.index_rows(rec["attestation_id"])[0]
    with pytest.raises(AttestRefused):
        attachments.write_rows(rec["attestation_id"], [{**row, "classification": None}])
    monkeypatch.delitem(registry._logbooks, LOG)  # the log's declaration, and so its labels, gone
    with pytest.raises(AttestRefused):
        attachments.index_record(rec)
    # The Library is the extraction path, never the attachment's record of truth.
    tree = ast.parse((ROOT / "src/axiom/extensions/builtins/attest/attachments.py").read_text())
    library = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and "library" in (n.module or "")}
    assert library <= {"axiom.extensions.builtins.library", "axiom.extensions.builtins.library.extract",
                       "axiom.extensions.builtins.library.extract.contract"}, library  # fmt: skip


# -- LD-4-A6: restore validation names the record whose blob does not resolve ---------------


@pytest.mark.acceptance("LD-4-A6")
def test_ld4_a6_restore_validation_names_each_record_whose_blob_does_not_resolve(att):
    rec, sha = att["att"], att["sha"]
    assert attachments.restore_findings() == []
    (att["blobs"] / attachments.blob_key(SITE, sha)).unlink()
    findings = attachments.restore_findings()
    assert [(f.problem, f.attestation_id, f.sha256) for f in findings] == [
        ("attachment_missing", rec["attestation_id"], sha)]
    assert rec["attestation_id"] in findings[0].line()
    from axiom.extensions.builtins.data_platform.skills.backup_validate import _contributed_checks

    check = next(c for c in _contributed_checks() if c.name == "attest_attachments_resolve")
    assert check.status.value == "FAIL" and rec["attestation_id"] in check.detail
    ctx = _ctx(att["state"], ME)
    result = invoke_capability(ctx.registry, "attest.blobs_check", {}, ctx, surface="cli")
    assert not result.ok and rec["attestation_id"] in result.errors[0]


# -- LD-4-A7: the provider is configuration -------------------------------------------------

_VENDORS = ("s3", "boto", "botocore", "aws", "amazonaws", "minio", "seaweed", "gcs", "google",
            "azure", "blob.core", "LocalStorageProvider", "S3StorageProvider", "file://")  # fmt: skip


@pytest.mark.acceptance("LD-4-A7")
def test_ld4_a7_no_provider_or_vendor_name_in_attest_code_or_in_the_record(att, tmp_path, monkeypatch):
    import re

    pattern = re.compile(r"\b(" + "|".join(re.escape(v) for v in _VENDORS) + r")\b", re.I)
    found = []
    for base in ("src/axiom/extensions/builtins/attest", "src/axiom/attest"):
        for path in (ROOT / base).rglob("*.py"):
            if "/tests/" in path.as_posix():
                continue
            found += [f"{path.name}: {m}" for m in pattern.findall(path.read_text(encoding="utf-8"))]
    assert found == [], found
    assert not pattern.search(json.dumps(att["att"]))
    assert set(attachments.refs_of(att["att"])[0]) <= attachments.REF_KEYS
    # Which provider holds the blobs is configuration.
    monkeypatch.setenv("AXIOM_STORAGE_ATTEST_BLOBS", json.dumps({"provider": "local", "base_dir": str(tmp_path / "x")}))
    assert Path(configured("attest-blobs", default_dir=tmp_path / "y").base_dir) == tmp_path / "x"
    monkeypatch.setenv("AXIOM_STORAGE_ATTEST_BLOBS", json.dumps({"provider": "nowhere"}))
    with pytest.raises(ValueError):
        configured("attest-blobs", default_dir=tmp_path)


# -- LD-4-A8: a video names a rendition and the policy that produced it ----------------------


@pytest.mark.acceptance("LD-4-A8")
def test_ld4_a8_a_video_names_its_rendition_policy_and_loss_inside_the_signed_bytes(world, blob_store, node_signer):
    with pytest.raises(AttestRefused):
        attachments.make_ref(CLIP, "video/mp4")  # a video is a rendition, or it is refused
    with pytest.raises(AttestRefused):
        attachments.make_ref(CLIP, "video/mp4", rendition="720p", policy_version="stream-loss/3")
    rec = _signed(node_signer, (CLIP, "video/mp4", dict(RENDITION)))
    ref = attachments.refs_of(rec)[0]
    assert ref["sha256"] == attachments.sha256_of(CLIP)  # the rendition's own hash
    assert {k: ref[k] for k in RENDITION} == RENDITION
    for key, value in (("policy_version", "stream-loss/4"), ("loss", {"lossy": False})):
        forged = json.loads(json.dumps(rec))
        forged["evidence"]["attachments"][0][key] = value
        assert digest(forged) != rec["digest"], key


# -- must refuse ----------------------------------------------------------------------------


@pytest.mark.acceptance("LD-4-A2")
def test_ld4_an_attachment_changed_after_presenting_is_refused_at_signing(world, blob_store, node_signer):
    from axiom.extensions.builtins.attest.tests.test_signing import person

    draft_id = _round_check()
    pres = service.present(draft_id)
    attachments.attach(draft_id, PHOTO, "image/png", by=ME)
    with pytest.raises(AttestRefused, match="present it again"):
        service.respond(pres.presentation_id, principal=person().principal, answer="sign", via="cli")
        service.sign(pres.presentation_id, signatory=person(), signer=node_signer)


@pytest.mark.acceptance("LD-4-A3")
def test_ld4_scripted_media_and_someone_elses_draft_are_refused(world, blob_store):
    draft_id = _round_check()
    for media in ("text/html", "image/svg+xml", "application/javascript", "nonsense"):
        with pytest.raises(AttestRefused):
            attachments.attach(draft_id, b"<script>x</script>", media, by=ME)
    with pytest.raises(AttestRefused, match="no draft"):
        attachments.attach(draft_id, PHOTO, "image/png", by="@op2:site-a")
