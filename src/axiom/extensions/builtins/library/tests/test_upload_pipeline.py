# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-213 end to end: resumable uploads, the six stages, failure isolation, routes, EC, re-extraction.

The catalog and the retrieval store are SQLite and bronze is a directory, as in
``test_library_upload`` (whose fixtures these reuse); nothing touches a
developer's database (ADR-174).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.extract.contract import (
    ExtractionResult,
    Registration,
    Table,
    TextSection,
    manifest_from_dict,
)
from axiom.extensions.builtins.library.extract.routes import (
    RouteOutcome,
    clear_routes,
    register_route,
)
from axiom.extensions.builtins.library.tests.test_library_upload import ALICE, MALLORY, _up

API = "/api/v1/library"


@pytest.fixture(autouse=True)
def _pipeline(monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    contrib.reset(rediscover=False)
    registry.clear_local()
    clear_routes()
    yield
    registry.clear_local()
    clear_routes()
    contrib.reset()


def _resumable(client, name, data: bytes, *, chunk=1024 * 1024, headers=ALICE, sha=True, **opts):
    body = {"file_name": name, "size": len(data), **opts}
    if sha:
        body["sha256"] = hashlib.sha256(data).hexdigest()
    r = client.post(f"{API}/uploads", json=body, headers=headers)
    assert r.status_code == 201, r.text
    s = r.json()
    off = 0
    while off < len(data):
        part = data[off: off + chunk]
        r = client.patch(
            f"{API}/uploads/{s['upload_id']}", content=part,
            headers={**headers, "Upload-Offset": str(off), "Upload-Checksum": "sha256 " + hashlib.sha256(part).hexdigest()},
        )
        assert r.status_code == 200, r.text
        off = r.json()["offset"]
    return s


def _job(client, job_id, headers=ALICE):
    r = client.get(f"{API}/jobs/{job_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _m(name, **kw):
    base = {"name": name, "version": "1.0.0", "summary": name, "outputs": ["metadata"], "match": {"extensions": [".wid"]}}
    base.update(kw)
    return manifest_from_dict(base, scope="pack")


# ----------------------------------------------------------------- resumable


def test_a_large_file_uploads_in_chunks_resumes_and_runs_every_stage(client, library, monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_CHUNK_BYTES", str(256 * 1024))
    rows = ["time,temp"] + [f"2026-01-01T00:00:{i % 60:02d}Z,{i}" for i in range(200_000)]
    data = "\n".join(rows).encode()
    assert len(data) > 4 * 1024 * 1024
    r = client.post(f"{API}/uploads", json={"file_name": "big run.csv", "size": len(data),
                                            "sha256": hashlib.sha256(data).hexdigest()}, headers=ALICE)
    s = r.json()
    assert s["chunk_size"] == 256 * 1024 and _job(client, s["job_id"])["stages"][0]["status"] == "running"
    first = data[: s["chunk_size"]]
    assert client.patch(f"{API}/uploads/{s['upload_id']}", content=first, headers={**ALICE, "Upload-Offset": "0"}).status_code == 200
    # A client that lost track sends from the wrong offset: told where the server is.
    r = client.patch(f"{API}/uploads/{s['upload_id']}", content=first, headers={**ALICE, "Upload-Offset": "0"})
    assert r.status_code == 409 and r.json()["detail"]["offset"] == len(first)
    # Resume: ask, then continue from there; a corrupted chunk is refused alone.
    off = client.get(f"{API}/uploads/{s['upload_id']}", headers=ALICE).json()["offset"]
    bad = client.patch(f"{API}/uploads/{s['upload_id']}", content=data[off: off + 10],
                       headers={**ALICE, "Upload-Offset": str(off), "Upload-Checksum": "sha256 " + "0" * 64})
    assert bad.status_code == 460
    job = _job(client, s["job_id"])
    assert job["bytes_received"] == off and 0 < job["progress"] < 0.5
    while off < len(data):
        r = client.patch(f"{API}/uploads/{s['upload_id']}", content=data[off: off + s["chunk_size"]],
                         headers={**ALICE, "Upload-Offset": str(off)})
        off = r.json()["offset"]
    assert r.json()["complete"] is True
    job = _job(client, s["job_id"])
    assert job["state"] == "done", job
    assert [st["status"] for st in job["stages"]] == ["done"] * 6 and job["progress"] == 1.0
    detail = client.get(f"{API}/items/{job['item_id']}/pipeline", headers=ALICE).json()
    ex = {e["extractor"]: e for e in detail["extractions"]}
    assert ex["core.extract.delimited"]["result"]["tables"][0]["row_count"] == 200_000
    assert detail["item"]["sha256"] == hashlib.sha256(data).hexdigest()
    # Bigger than the 2 MB text ceiling: a dataset, not a retrieval document.
    assert detail["item"]["index_status"] == "unsupported"
    assert any(r["tag"] == "dataset" and r["status"] == "done" for r in detail["routes"])


def test_declared_checksum_mismatch_fails_storing_and_keeps_nothing(client, library):
    data = b"hello there\n"
    r = client.post(f"{API}/uploads", json={"file_name": "a.txt", "size": len(data), "sha256": "a" * 64}, headers=ALICE)
    s = r.json()
    client.patch(f"{API}/uploads/{s['upload_id']}", content=data, headers={**ALICE, "Upload-Offset": "0"})
    job = _job(client, s["job_id"])
    assert job["state"] == "failed" and job["error"]["stage"] == "stored"
    assert "checksum" in job["error"]["message"] and job["error"]["remedy"]
    assert not list((library["root"] / "alpha" / "_incoming").glob("*.part"))


def test_the_declaration_is_guarded_before_any_byte(client, library):
    r = client.post(f"{API}/uploads", json={"file_name": "page.html", "size": 10}, headers=ALICE)
    assert r.status_code == 415
    r = client.post(f"{API}/uploads", json={"file_name": "huge.bin.csv", "size": 10**14}, headers=ALICE)
    assert r.status_code == 413


def test_cancel_and_other_peoples_sessions(client, library):
    s = client.post(f"{API}/uploads", json={"file_name": "a.txt", "size": 100}, headers=ALICE).json()
    assert client.get(f"{API}/uploads/{s['upload_id']}", headers=MALLORY).status_code == 404
    assert client.delete(f"{API}/uploads/{s['upload_id']}", headers=ALICE).json()["state"] == "cancelled"
    assert _job(client, s["job_id"])["state"] == "cancelled"
    assert client.get(f"{API}/jobs/{s['job_id']}", headers=MALLORY).status_code == 404


def test_the_status_stream_reports_every_job_and_ends(client, library):
    a = _resumable(client, "one.md", b"# One\n\nalpha text\n")
    b = _resumable(client, "two.md", b"# Two\n\nbeta text\n")
    with client.stream("GET", f"{API}/jobs/stream?ids={a['job_id']},{b['job_id']}", headers=ALICE) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {") and '"id"' in line]
    assert {e["id"] for e in events} == {a["job_id"], b["job_id"]} and "event: end" in body
    assert all(e["state"] == "done" for e in events)
    listed = client.get(f"{API}/jobs?ids={a['job_id']},{b['job_id']}", headers=ALICE).json()["jobs"]
    assert len(listed) == 2


# --------------------------------------------------------------- the stages


def test_multipart_uploads_run_the_same_pipeline(client, library):
    r = _up(client, "notes.md", b"# Notes\n\nThe zebracorn seal check.\n")
    item = r.json()["items"][0]
    assert item["job_id"] and item["index_status"] == "indexed" and item["pipeline_state"] == "done"
    assert item["folder"] == "documents" and item["placement"]["explanation"].startswith("Filed under Documents because")
    detail = client.get(f"{API}/items/{item['id']}/pipeline", headers=ALICE).json()
    names = {e["extractor"] for e in detail["extractions"]}
    assert {"core.extract.text", "core.extract.file_facts"} <= names
    assert any(r["tag"] == "rag" and r["status"] == "done" for r in detail["routes"])
    derived = list((library["root"] / "alpha" / "library" / "_derived" / item["id"]).glob("core.extract.text@1.0.0.json"))
    assert derived and json.loads(derived[0].read_text())["input_sha256"] == item["sha256"]


def test_one_extractor_failing_does_not_stop_the_others_and_the_file_is_kept(client, library):
    def boom(ctx):
        raise RuntimeError("parser bug")

    def good(ctx):
        return ExtractionResult(metadata={"run": "42"}, text=[TextSection("widget run 42 notes")], summary="widget log")

    registry.register(_m("pack.extract.boom", priority=5), boom)
    registry.register(_m("pack.extract.good", outputs=["metadata", "text"], tags=["rag"],
                         accepts=[{"extension": ".wid", "sniff": "text"}]), good)
    item = _up(client, "run.wid", b"WIDGET\n").json()["items"][0]
    assert item["pipeline_state"] == "partial" and item["index_status"] == "indexed"
    detail = client.get(f"{API}/items/{item['id']}/pipeline", headers=ALICE).json()
    by = {e["extractor"]: e for e in detail["extractions"]}
    assert by["pack.extract.boom"]["status"] == "failed" and "parser bug" in by["pack.extract.boom"]["error"]
    assert by["pack.extract.boom"]["remedy"] and by["pack.extract.good"]["status"] == "done"
    job = detail["job"]
    assert job["state"] == "partial" and "parser bug" in job["error"]["message"]
    assert client.get(f"{API}/items/{item['id']}/content", headers=ALICE).content == b"WIDGET\n"

    # Fix the bug; retry re-runs from bronze.
    registry.register(_m("pack.extract.boom", priority=5), good)
    again = client.post(f"{API}/jobs/{job['id']}/retry", headers=ALICE).json()
    assert _job(client, again["id"])["state"] == "done"


def test_archive_members_are_identified_and_extracted(client, library):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes/plan.md", "# Plan\n\nThe quokka procedure.\n")
        z.writestr("data/run.csv", "t,v\n1,2\n2,3\n")
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zi:
            zi.writestr("deep.md", "# Deep\n\nnested text\n")
        z.writestr("inner.zip", inner.getvalue())
    item = _up(client, "bundle.zip", buf.getvalue()).json()["items"][0]
    detail = client.get(f"{API}/items/{item['id']}/pipeline", headers=ALICE).json()
    members = {(e["extractor"], e["member"]) for e in detail["extractions"] if e["status"] == "done"}
    assert ("core.extract.text", "notes/plan.md") in members
    assert ("core.extract.delimited", "data/run.csv") in members
    assert ("core.extract.text", "inner.zip!/deep.md") in members
    assert item["folder"] == "archives" and item["index_status"] == "indexed"
    found = client.get(f"{API}/search?q=quokka", headers=ALICE).json()["results"]
    assert [f["id"] for f in found] == [item["id"]]


# ------------------------------------------------------------------- routes


def test_tags_route_to_registered_handlers_and_missing_ones_say_so(client, library):
    seen = []

    def deck(ctx):
        return ExtractionResult(registrations=[Registration("input-deck", {"code": "widgetsim"})],
                                tables=[Table("cells", ["id", "mat"], [[1, 2]], row_count=1)], summary="deck")

    registry.register(_m("pack.extract.deck", outputs=["registrations", "tables"], tags=["input-deck", "dataset", "training-data"],
                         accepts=[{"extension": ".wid", "sniff": "text"}]), deck)
    item = _up(client, "a.wid", b"deck\n").json()["items"][0]
    routes = client.get(f"{API}/items/{item['id']}/pipeline", headers=ALICE).json()["routes"]
    by = {(r["tag"], r["status"]) for r in routes}
    assert ("input-deck", "no-route") in by and ("dataset", "done") in by and ("training-data", "done") in by

    def corral(ctx):
        seen.append((ctx.item["id"], ctx.registration.payload["code"], ctx.extractor))
        return RouteOutcome("done", target="registry", link={"kind": "corral", "label": "deck 1", "ref": "deck-1"})

    def broken(ctx):
        raise RuntimeError("registry down")

    register_route("input-deck", "pack.route.registry", corral)
    register_route("dataset", "pack.route.conform", broken)
    item2 = _up(client, "b.wid", b"deck two\n").json()["items"][0]
    routes = client.get(f"{API}/items/{item2['id']}/pipeline", headers=ALICE).json()["routes"]
    assert seen == [(item2["id"], "widgetsim", "pack.extract.deck")]
    link = next(r for r in routes if r["route"] == "pack.route.registry")["link"]
    assert link == {"kind": "corral", "label": "deck 1", "ref": "deck-1"}
    failed = next(r for r in routes if r["route"] == "pack.route.conform")
    assert failed["status"] == "failed" and "registry down" in failed["detail"]
    assert client.get(f"{API}/items/{item2['id']}", headers=ALICE).json()["pipeline_state"] == "partial"


# ---------------------------------------------------------------------- EC


def test_an_input_to_a_controlled_code_is_held_from_retrieval(client, library):
    contrib.add("controlled_codes", {"code": "widgetsim", "basis": "declared for this test"})

    def deck(ctx):
        return ExtractionResult(text=[TextSection("cells and the secret zebrafinch method")], code="widgetsim")

    registry.register(_m("pack.extract.deck", outputs=["text"], tags=["rag", "input-deck"], ec="payload",
                         supersedes=["core.extract.text"], accepts=[{"extension": ".wid", "sniff": "text"}]), deck)
    item = _up(client, "a.wid", b"zebrafinch\n").json()["items"][0]
    assert item["index_status"] == "held" and item["ec"]["verdict"] == "held" and item["ec"]["code"] == "widgetsim"
    assert item["pipeline_state"] == "held"
    assert client.get(f"{API}/search?q=zebrafinch", headers=ALICE).json()["results"] == []
    # Listed and downloadable: held is not lost.
    assert [i["id"] for i in client.get(f"{API}/items", headers=ALICE).json()["items"]] == [item["id"]]


def test_a_control_marking_holds_and_a_distribution_marking_flags(client, library):
    held = _up(client, "memo.md", b"# Memo\n\nExport Controlled Information. Do not share.\n").json()["items"][0]
    assert held["index_status"] == "held" and "control marking" in held["ec"]["reasons"][0]
    flagged = _up(client, "notes.md", b"# Notes\n\nOFFICIAL USE ONLY\n\nsome text\n").json()["items"][0]
    assert flagged["ec"]["verdict"] in ("sensitive", "held")


def test_generic_text_cannot_carry_a_held_deck_past_the_verdict(client, library):
    contrib.add("controlled_codes", {"code": "widgetsim"})

    def deck(ctx):
        return ExtractionResult(text=[TextSection("deck text")], code="widgetsim")

    # No supersedes: the core text extractor also reads it. The hold is for the whole item.
    registry.register(_m("pack.extract.deck", outputs=["text"], tags=["rag"], ec="payload", match={"extensions": [".md"]}), deck)
    item = _up(client, "deck.md", b"# deck\n\nkoalabear\n").json()["items"][0]
    assert item["index_status"] == "held"
    assert client.get(f"{API}/search?q=koalabear", headers=ALICE).json()["results"] == []


# ------------------------------------------------------------ re-extraction


def test_a_new_extractor_version_makes_items_stale_and_reextract_replaces_in_place(client, library):
    def v1(ctx):
        return ExtractionResult(metadata={"v": 1}, text=[TextSection("The first version of this extractor read the widget log and found the pump readings for the morning run.")])

    def v2(ctx):
        return ExtractionResult(metadata={"v": 2}, text=[TextSection("The second version of this extractor read the widget log and found the wombat readings for the morning run.")])

    accepts = [{"extension": ".wid", "sniff": "text"}]
    registry.register(_m("pack.extract.w", outputs=["metadata", "text"], tags=["rag"], accepts=accepts), v1)
    item = _up(client, "a.wid", b"x\n").json()["items"][0]
    assert client.get(f"{API}/stale", headers=ALICE).json()["items"] == []
    registry.register(_m("pack.extract.w", version="2.0.0", outputs=["metadata", "text"], tags=["rag"], accepts=accepts), v2)
    stale = client.get(f"{API}/stale", headers=ALICE).json()["items"]
    assert stale == [{"id": item["id"], "file_name": "a.wid", "stale": [{"extractor": "pack.extract.w", "from": "1.0.0", "to": "2.0.0"}]}]
    job = client.post(f"{API}/items/{item['id']}/reextract", json={"stale_only": True}, headers=ALICE).json()
    assert _job(client, job["id"])["state"] == "done"
    detail = client.get(f"{API}/items/{item['id']}/pipeline", headers=ALICE).json()
    rows = [e for e in detail["extractions"] if e["extractor"] == "pack.extract.w"]
    assert [(e["version"], e["current"]) for e in rows] == [("2.0.0", True), ("1.0.0", False)]
    # Other extractors' results survive a partial re-extraction, and retrieval has the new text.
    assert any(e["extractor"] == "core.extract.file_facts" and e["current"] for e in detail["extractions"])
    assert [r["id"] for r in client.get(f"{API}/search?q=wombat", headers=ALICE).json()["results"]] == [item["id"]]
    assert client.get(f"{API}/stale", headers=ALICE).json()["items"] == []


def test_registry_listings(client, library):
    ex = client.get(f"{API}/extractors").json()["extractors"]
    names = {e["name"] for e in ex}
    assert {"core.extract.pdf", "core.extract.archive", "core.extract.hdf5"} <= names
    assert all("available" in e for e in ex)
    pv = client.get(f"{API}/previewers").json()["previewers"]
    assert {"core.preview.media", "core.preview.table", "core.preview.text"} <= {p["name"] for p in pv}
    lim = client.get(f"{API}/limits").json()
    assert ".h5" in lim["accepted_extensions"] and ".svg" not in lim["accepted_extensions"]


def test_routes_may_decline_results_may_drop_tags_and_extractors_see_siblings_and_params(client, library):
    import hashlib as _h

    seen: dict = {}

    def member(ctx):
        seen["sibling"] = ctx.sibling("channels*.csv")
        seen["params"] = dict(ctx.params)
        return ExtractionResult(tables=[Table("t", ["a"], [[1]], row_count=1)], drop_tags=["rag"])

    registry.register(_m("pack.extract.samples", outputs=["tables"], tags=["dataset", "rag"],
                         match={"names": ["samples*.txt"]}), member)
    register_route("dataset", "pack.route.not_mine", lambda ctx: None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("samples-1.txt", "1 2 3\n")
        z.writestr("channels-1.csv", "name\nT1\n")
    data = buf.getvalue()
    s = client.post(f"{API}/uploads", json={"file_name": "run.zip", "size": len(data), "sha256": _h.sha256(data).hexdigest(),
                                            "extract_params": {"start": "2026-01-01T00:00:00Z"}}, headers=ALICE).json()
    client.patch(f"{API}/uploads/{s['upload_id']}", content=data, headers={**ALICE, "Upload-Offset": "0"})
    job = _job(client, s["job_id"])
    assert job["state"] == "done", job
    assert seen["sibling"] is not None and seen["sibling"].name == "channels-1.csv"
    assert seen["params"] == {"start": "2026-01-01T00:00:00Z"}
    routes = client.get(f"{API}/items/{job['item_id']}/pipeline", headers=ALICE).json()["routes"]
    assert not any(r["route"] == "pack.route.not_mine" for r in routes)
    assert not any(r["status"] == "no-route" for r in routes)


def test_an_empty_extension_accept_does_not_widen_the_allowlist():
    from axiom.extensions.builtins.library.extract.registry import accepted_types

    registry.register(_m("pack.extract.any", accepts=[{"extension": "", "sniff": "text"}]), lambda c: ExtractionResult())
    assert "" not in accepted_types()
