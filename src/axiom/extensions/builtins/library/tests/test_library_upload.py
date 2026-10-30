# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Upload to bronze to catalog to retrieval, scoped per site and per person.

The whole path runs for real except the embedding call: the catalog is SQLite,
the retrieval store is the SQLite store (keyword search), and bronze is a
directory. Nothing touches a developer's database (ADR-174: SQLite in tests).
"""

from __future__ import annotations

import io
import json
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins.library import store as lib_store
from axiom.extensions.builtins.library.db_models import Base, LibraryEvent, LibraryItem

DOC = b"# Pump maintenance\n\nThe quarterly zebracorn inspection checks the seals and the bearings.\n"


@pytest.fixture
def library(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def provider():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    from axiom.rag import store_factory

    store_factory.allow_sqlite_for_tests(True)
    rag_url = f"sqlite:///{tmp_path / 'rag.db'}"

    def open_rag():
        # A connection per call: SQLite connections are bound to their thread,
        # and the app runs handlers on worker threads.
        s = store_factory.create_store(rag_url)
        s.connect()
        return s

    rag = open_rag()

    from axiom.extensions.builtins.data_platform.rag_embed import embedder

    monkeypatch.setattr(embedder, "embed_texts", lambda texts: None)
    monkeypatch.setattr("axiom.rag.embeddings.embed_texts", lambda texts: None)
    monkeypatch.setenv("AXIOM_LIBRARY_BRONZE_ROOT", str(tmp_path / "bronze"))
    monkeypatch.setenv("AXIOM_LIBRARY_TAXONOMY", "generic")
    monkeypatch.setenv("AXIOM_SITE", "alpha")
    monkeypatch.delenv("AXIOM_LIBRARY_SCAN_CMD", raising=False)
    monkeypatch.setattr("axiom.extensions.builtins.library.forward.bound_node", lambda: None)

    lib_store.set_provider(provider)
    lib_store.set_rag_store(open_rag)
    yield {"factory": factory, "rag": rag, "open_rag": open_rag, "root": tmp_path / "bronze"}
    lib_store.reset_provider()
    lib_store.set_rag_store(None)
    store_factory.allow_sqlite_for_tests(False)


@pytest.fixture
def client(library):
    """The library router behind a stand-in for the serving stack's authz hook.

    The hook sets ``request.state.principal`` and binds the credential's site;
    here two headers play the credential, so a test can be two people at two sites.
    """
    from axiom.extensions.builtins.library.api import build_router
    from axiom.infra.site_authority import bind_caller_site, unbind_caller_site

    app = FastAPI()

    @app.middleware("http")
    async def verified(request: Request, call_next):
        request.state.principal = request.headers.get("x-test-principal") or None
        token = bind_caller_site(request.headers.get("x-test-site") or None)
        try:
            return await call_next(request)
        finally:
            unbind_caller_site(token)

    app.router.routes.extend(build_router().routes)
    return TestClient(app)


ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
ALICE_COLLEAGUE = {"x-test-principal": "@bob:alpha", "x-test-site": "alpha"}
MALLORY = {"x-test-principal": "@mallory:beta", "x-test-site": "beta"}


def _up(client, name, data, headers=ALICE, **fields):
    return client.post("/api/v1/library/items", files=[("files", (name, data))], data=fields, headers=headers)


# --- the path -------------------------------------------------------------------


def test_an_upload_lands_in_bronze_is_catalogued_and_is_findable_by_its_contents(client, library):
    r = _up(client, "pump-notes.md", DOC, tags="maintenance,seals", collection="Plant")
    assert r.status_code == 201, r.text
    item = r.json()["items"][0]
    assert item["site"] == "alpha" and item["owner"] == "@alice:alpha"
    assert item["visibility"] == "site" and item["corpus"] == "rag-org"
    assert item["category"] == "documents" and item["index_status"] == "indexed"
    assert item["tags"] == ["maintenance", "seals"] and item["collection"] == "Plant"
    assert "bronze_content" not in item  # storage paths stay on the server

    # bronze: a sidecar under the site's own prefix, and the content-addressed blob
    records = list((library["root"] / "alpha" / "library" / "_records").rglob("*.json"))
    assert len(records) == 1
    sidecar = json.loads(records[0].read_text())
    assert sidecar["extra"]["owner"] == "@alice:alpha" and sidecar["extra"]["site"] == "alpha"
    assert sidecar["content_sha256"] == item["sha256"] and sidecar["tier"] == "rag-org"
    assert (library["root"] / "alpha" / "_content" / item["sha256"][:2] / item["sha256"]).read_bytes() == DOC

    # the catalog row and its audit event
    with library["factory"]() as s:
        assert s.get(LibraryItem, item["id"]).chunk_count >= 1
        assert [e.action for e in s.query(LibraryEvent).filter_by(item_id=item["id"])] == ["upload"]

    # retrieval: the chunks are in the site corpus, and library search finds the document
    found = client.get("/api/v1/library/search", params={"q": "zebracorn"}, headers=ALICE).json()
    assert [h["id"] for h in found["results"]] == [item["id"]]
    assert "zebracorn" in found["results"][0]["snippet"]

    listed = client.get("/api/v1/library/items", headers=ALICE).json()
    assert [i["id"] for i in listed["items"]] == [item["id"]]
    body = client.get(f"/api/v1/library/items/{item['id']}/content", headers=ALICE)
    assert body.content == DOC
    assert body.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in body.headers["content-security-policy"]


def test_the_same_bytes_twice_are_filed_once(client):
    first = _up(client, "a.md", DOC).json()["items"][0]
    again = _up(client, "a-copy.md", DOC).json()["items"][0]
    assert again["id"] == first["id"] and again["duplicate"] is True


def test_classify_previews_without_writing(client, library):
    r = client.post("/api/v1/library/classify", files=[("files", ("deck.pptx", b"x"))], headers=ALICE)
    assert r.status_code == 200
    preview = r.json()["files"][0]
    assert preview["accepted"] is False  # not a real pptx container
    ok = client.post("/api/v1/library/classify", files=[("files", ("n.md", DOC))], headers=ALICE).json()["files"][0]
    assert ok["accepted"] and ok["category"] == "documents"
    with library["factory"]() as s:
        assert s.query(LibraryItem).count() == 0


# --- tenancy --------------------------------------------------------------------


def test_another_site_cannot_list_read_search_or_delete(client):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    assert client.get("/api/v1/library/items", headers=MALLORY).json()["items"] == []
    assert client.get(f"/api/v1/library/items/{item['id']}", headers=MALLORY).status_code == 404
    assert client.get(f"/api/v1/library/items/{item['id']}/content", headers=MALLORY).status_code == 404
    assert client.get("/api/v1/library/search", params={"q": "zebracorn"}, headers=MALLORY).json()["results"] == []
    assert client.delete(f"/api/v1/library/items/{item['id']}", headers=MALLORY).status_code == 404


def test_a_tenant_upload_lands_in_its_own_prefix_and_corpus(client, library):
    item = _up(client, "theirs.md", DOC, headers=MALLORY).json()["items"][0]
    assert item["site"] == "beta" and item["corpus"] == "rag-tenant:beta"
    assert list((library["root"] / "beta").rglob("*.json"))
    assert not (library["root"] / "alpha").exists()
    # the same bytes at another site are a separate document, not a duplicate
    mine = _up(client, "mine.md", DOC).json()["items"][0]
    assert mine["id"] != item["id"] and not mine["duplicate"]


def test_a_private_upload_is_its_owners_alone(client, library):
    item = _up(client, "private.md", DOC, visibility="private").json()["items"][0]
    assert item["corpus"] == "rag-personal:alice-alpha"
    assert client.get("/api/v1/library/items", headers=ALICE_COLLEAGUE).json()["items"] == []
    assert client.get(f"/api/v1/library/items/{item['id']}", headers=ALICE_COLLEAGUE).status_code == 404
    assert client.get("/api/v1/library/search", params={"q": "zebracorn"}, headers=ALICE_COLLEAGUE).json()["results"] == []
    assert [h["id"] for h in client.get("/api/v1/library/search", params={"q": "zebracorn"}, headers=ALICE).json()["results"]] == [item["id"]]


def test_a_colleague_reads_a_site_document_but_cannot_change_it(client):
    item = _up(client, "shared.md", DOC).json()["items"][0]
    assert client.get(f"/api/v1/library/items/{item['id']}", headers=ALICE_COLLEAGUE).status_code == 200
    assert client.delete(f"/api/v1/library/items/{item['id']}", headers=ALICE_COLLEAGUE).status_code == 403
    assert client.patch(f"/api/v1/library/items/{item['id']}", json={"tags": ["x"]}, headers=ALICE_COLLEAGUE).status_code == 403


def test_no_principal_is_refused(client):
    r = client.post("/api/v1/library/items", files=[("files", ("a.md", DOC))])
    assert r.status_code == 401


# --- delete, restore, visibility -----------------------------------------------


def test_delete_removes_from_retrieval_and_listings_keeps_bytes_and_restore_brings_it_back(client, library):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    gone = client.delete(f"/api/v1/library/items/{item['id']}", headers=ALICE)
    assert gone.status_code == 200 and gone.json()["deleted_at"]
    assert client.get("/api/v1/library/items", headers=ALICE).json()["items"] == []
    assert client.get("/api/v1/library/search", params={"q": "zebracorn"}, headers=ALICE).json()["results"] == []
    assert library["open_rag"]().get_document(item_id_path(item), corpus="rag-org") is None
    tomb = list((library["root"] / "alpha" / "library" / "_deleted").rglob(f"{item['id']}.json"))
    assert tomb and json.loads(tomb[0].read_text())["deleted_by"] == "@alice:alpha"
    assert (library["root"] / "alpha" / "_content" / item["sha256"][:2] / item["sha256"]).exists()
    deleted = client.get("/api/v1/library/items", params={"deleted": True}, headers=ALICE).json()["items"]
    assert [d["id"] for d in deleted] == [item["id"]]

    back = client.post(f"/api/v1/library/items/{item['id']}/restore", headers=ALICE)
    assert back.status_code == 200 and back.json()["index_status"] == "indexed"
    assert [h["id"] for h in client.get("/api/v1/library/search", params={"q": "zebracorn"}, headers=ALICE).json()["results"]] == [item["id"]]
    with library["factory"]() as s:
        actions = [e.action for e in s.query(LibraryEvent).filter_by(item_id=item["id"]).order_by(LibraryEvent.id)]
    assert actions == ["upload", "delete", "restore"]


def item_id_path(item):
    return f"library/{item['site']}/{item['id']}/{item['file_name']}"


def test_making_a_document_private_moves_its_chunks(client, library):
    item = _up(client, "pump.md", DOC).json()["items"][0]
    moved = client.patch(f"/api/v1/library/items/{item['id']}", json={"visibility": "private"}, headers=ALICE).json()
    assert moved["corpus"] == "rag-personal:alice-alpha"
    assert library["open_rag"]().get_document(item_id_path(item), corpus="rag-org") is None
    assert library["open_rag"]().get_document(item_id_path(item), corpus="rag-personal:alice-alpha") is not None


# --- guards ---------------------------------------------------------------------


def test_a_file_over_the_cap_is_413(client, monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_MAX_FILE_BYTES", "64")
    r = _up(client, "big.md", b"x" * 200)
    assert r.status_code == 413


def test_an_unaccepted_type_is_415(client):
    r = _up(client, "tool.exe", b"MZ\x90\x00")
    assert r.status_code == 415
    assert "not accepted" in r.json()["refused"][0]["reason"]


def test_script_bearing_documents_are_not_accepted(client):
    assert _up(client, "x.svg", b"<svg onload=alert(1)>").status_code == 415
    assert _up(client, "x.html", b"<script>alert(1)</script>").status_code == 415


def test_content_that_contradicts_its_name_is_refused(client):
    assert _up(client, "report.pdf", b"not a pdf at all").status_code == 422
    assert _up(client, "notes.txt", b"\x00\x01binary").status_code == 422


def test_a_traversal_name_is_reduced_to_its_basename(client, library):
    item = _up(client, "../../../etc/passwd.txt", b"root:x:0:0\n").json()["items"][0]
    assert item["file_name"] == "passwd.txt"
    for path in library["root"].rglob("*"):
        assert library["root"] in path.resolve().parents or path.resolve() == library["root"]


def _docx(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_a_zip_bomb_dressed_as_a_document_is_refused(client):
    bomb = _docx({"[Content_Types].xml": b"<x/>", "word/document.xml": b"\x00" * (20 * 1024 * 1024)})
    r = _up(client, "harmless.docx", bomb)
    assert r.status_code == 422 and "bomb" in r.json()["refused"][0]["reason"]


def test_a_container_part_with_an_unsafe_path_is_refused(client):
    r = _up(client, "x.docx", _docx({"../../evil": b"x"}))
    assert r.status_code == 422


def test_a_configured_scanner_that_refuses_blocks_the_upload(client, monkeypatch):
    monkeypatch.setenv("AXIOM_LIBRARY_SCAN_CMD", "false")
    assert _up(client, "a.md", DOC).status_code == 422
    monkeypatch.setenv("AXIOM_LIBRARY_SCAN_CMD", "true")
    assert _up(client, "a.md", DOC).json()["items"][0]["scan"] == "clean"


def test_limits_are_served(client):
    body = client.get("/api/v1/library/limits", headers=ALICE).json()
    assert body["max_file_bytes"] > 0 and ".pdf" in body["accepted_extensions"]
    assert ".svg" not in body["accepted_extensions"] and body["taxonomy"] == "generic"


# --- bound mode -----------------------------------------------------------------


def test_a_bound_install_relays_to_its_node_and_keeps_nothing(client, library, monkeypatch):
    from axiom.extensions.builtins.library import forward

    node = forward.BoundNode(site="alpha", face_url="https://node.example", node="node.example")
    monkeypatch.setattr(forward, "bound_node", lambda: node)
    monkeypatch.setattr(forward, "_key", lambda face: "axk_test")
    seen = {}

    class Answer:
        status_code = 201
        content = b'{"items": [], "refused": []}'
        headers = {"content-type": "application/json"}

    def fake_request(method, url, **kw):
        seen.update(method=method, url=url, headers=kw["headers"], content=kw["content"])
        return Answer()

    import httpx

    monkeypatch.setattr(httpx, "request", fake_request)
    r = _up(client, "pump.md", DOC)
    assert r.status_code == 201 and r.headers["x-axiom-served-by"] == "node.example"
    assert seen["method"] == "POST" and seen["url"] == "https://node.example/api/v1/library/items"
    assert seen["headers"]["authorization"] == "Bearer axk_test"
    assert b"zebracorn" in seen["content"]
    assert not library["root"].exists()
    with library["factory"]() as s:
        assert s.query(LibraryItem).count() == 0


def test_an_unreachable_node_is_a_503_that_names_it(client, monkeypatch):
    import httpx

    from axiom.extensions.builtins.library import forward

    monkeypatch.setattr(forward, "bound_node", lambda: forward.BoundNode("alpha", "https://node.example", "node.example"))

    def down(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "request", down)
    r = _up(client, "pump.md", DOC)
    assert r.status_code == 503 and "node.example" in r.json()["detail"]


# --- the other surfaces -----------------------------------------------------------


def test_the_skills_file_list_and_search_through_the_same_service(library, tmp_path):
    import logging

    from axiom.extensions.builtins.library.skills import bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    reg = bind_default()
    ctx = SkillContext(
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"),
        principal=PrincipalContext(handle="@carol:alpha"), surface="mcp",
    )
    src = tmp_path / "notes.md"
    src.write_bytes(DOC)
    up = reg.invoke("library.upload", {"path": str(src), "tags": ["a"]}, ctx)
    assert up.ok, up.errors
    assert up.value["owner"] == "@carol:alpha" and up.value["site"] == "alpha"
    listed = reg.invoke("library.list", {}, ctx)
    assert [i["id"] for i in listed.value["items"]] == [up.value["id"]]
    hits = reg.invoke("library.search", {"q": "zebracorn"}, ctx)
    assert [h["id"] for h in hits.value["results"]] == [up.value["id"]]
    assert reg.invoke("library.show", {"id": "nope"}, ctx).ok is False


def test_the_mount_nests_in_the_api_and_runs_the_authz_hook():
    from axiom.extensions.builtins.library.mount import mount_spec

    spec = mount_spec()
    assert spec.prefix == "/api/v1/library" and spec.requires_authz
    assert spec.nests_in == ("/api/v1",)
    assert spec.reads_path("/api/v1/library/classify")
    assert not spec.reads_path("/api/v1/library/items")
    paths = {r.path for r in spec.router.routes}
    assert {"/api/v1/library/items", "/api/v1/library/search", "/api/v1/library/items/{item_id}/content"} <= paths


def test_every_manifest_entry_imports():
    import importlib
    import tomllib

    manifest = Path(__file__).resolve().parents[1] / "axiom-extension.toml"
    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    for block in data["extension"]["provides"]:
        module_path, _, attr = block["entry"].partition(":")
        assert hasattr(importlib.import_module(module_path), attr), block["entry"]
