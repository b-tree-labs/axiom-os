# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""W11: export-control labels and retention classes on Library items, end to end.

The catalog and the retrieval store are SQLite and bronze is a directory, as in
``test_library_upload`` (whose fixtures these reuse); nothing touches a
developer's database (ADR-174). The ``library_labels`` flag is pinned on through
its environment override. Test names carry the acceptance row they prove.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.library import contrib
from axiom.extensions.builtins.library.db_models import LibraryEvent, LibraryItem
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.extract.routes import clear_routes
from axiom.extensions.builtins.library.tests.test_library_upload import ALICE, ALICE_COLLEAGUE, DOC, _up
from axiom.governance import classification as gc

API = "/api/v1/library"


@pytest.fixture(autouse=True)
def _labels_on(library, monkeypatch):  # after `library`, which pins the flag off
    monkeypatch.setenv("AXIOM_LIBRARY_PIPELINE_INLINE", "1")
    monkeypatch.setenv("AXIOM_FLAG_LIBRARY_LABELS", "on")
    contrib.reset(rediscover=False)
    registry.clear_local()
    clear_routes()
    yield
    gc.set_clearance_resolver(None)
    registry.clear_local()
    clear_routes()
    contrib.reset()


def _search(client, q="zebracorn", headers=ALICE):
    r = client.get(f"{API}/search", params={"q": q}, headers=headers)
    assert r.status_code == 200, r.text
    return [h["id"] for h in r.json()["results"]]


def _source_path(library, item_id):
    with library["factory"]() as s:
        return s.get(LibraryItem, item_id).source_path


def _classifications(library, item_id):
    path = _source_path(library, item_id)
    store = library["open_rag"]()
    return [r[0] for r in store._conn.execute("SELECT classification FROM chunks WHERE source_path = ?", (path,))]


# ------------------------------------------------------------------ ingest gate


def test_mvp_16_a2_an_upload_with_no_confirmed_label_is_not_ingested(client, library):
    r = _up(client, "pump-notes.md", DOC)
    assert r.status_code == 201, r.text
    item = r.json()["items"][0]
    assert item["index_status"] == "awaiting-label", item
    assert item["ec_label"]["value"] == "internal" and item["ec_label"]["confirmed"] is False
    assert "confirm" in item["index_reason"]
    assert _classifications(library, item["id"]) == []  # nothing reached the corpus
    assert _search(client) == []

    # Confirming the label ingests it, from the retrieval document already written.
    r = client.post(f"{API}/items/{item['id']}/label", json={"label": "internal"}, headers=ALICE)
    assert r.status_code == 200, r.text
    labelled = r.json()
    assert labelled["index_status"] == "indexed" and labelled["ec_label"]["confirmed"] is True
    assert labelled["ec_label"]["by"] == "@alice:alpha"
    assert set(_classifications(library, item["id"])) == {"internal"}
    assert _search(client) == [item["id"]]
    with library["factory"]() as s:
        actions = [e.action for e in s.query(LibraryEvent).filter_by(item_id=item["id"]).order_by(LibraryEvent.id)]
    assert actions == ["upload", "label"]


def test_mvp_16_a2_a_label_stated_at_upload_is_a_confirmation_and_ingests(client, library):
    item = _up(client, "pump-notes.md", DOC, ec_label="internal").json()["items"][0]
    assert item["index_status"] == "indexed"
    assert item["ec_label"] == {**item["ec_label"], "value": "internal", "state": "confirmed", "by": "@alice:alpha"}
    assert _search(client) == [item["id"]]


def test_an_unknown_label_is_refused_before_anything_is_stored(client, library):
    r = _up(client, "pump-notes.md", DOC, ec_label="top-secret-ish")
    assert r.status_code == 422 and "candidates" in r.json()["refused"][0]["reason"]
    with library["factory"]() as s:
        assert s.query(LibraryItem).count() == 0


def test_only_the_person_who_filed_it_confirms_its_label(client, library):
    item = _up(client, "pump-notes.md", DOC).json()["items"][0]
    r = client.post(f"{API}/items/{item['id']}/label", json={"label": "internal"}, headers=ALICE_COLLEAGUE)
    assert r.status_code == 403


# ------------------------------------------------------------------ export and retrieval


def test_mvp_22_a1_an_item_without_a_label_cannot_be_exported_or_retrieved(client, library):
    item = _up(client, "pump-notes.md", DOC).json()["items"][0]
    r = client.get(f"{API}/items/{item['id']}/content", params={"download": True}, headers=ALICE)
    assert r.status_code == 403 and "export-control label" in r.json()["detail"]
    assert _search(client) == []
    # Previewing it (to decide the label) is not an export.
    assert client.get(f"{API}/items/{item['id']}/content", headers=ALICE).status_code == 200

    client.post(f"{API}/items/{item['id']}/label", json={"label": "internal"}, headers=ALICE)
    r = client.get(f"{API}/items/{item['id']}/content", params={"download": True}, headers=ALICE)
    assert r.status_code == 200 and r.content == DOC
    assert _search(client) == [item["id"]]


def test_mvp_22_a1_the_sweep_takes_legacy_unlabelled_items_out_of_retrieval(client, library, monkeypatch):
    from axiom.extensions.builtins.library import service
    from axiom.extensions.builtins.library.scope import Caller

    monkeypatch.setenv("AXIOM_FLAG_LIBRARY_LABELS", "off")  # filed before the flag turned on
    item = _up(client, "pump-notes.md", DOC).json()["items"][0]
    assert item["index_status"] == "indexed"
    monkeypatch.setenv("AXIOM_FLAG_LIBRARY_LABELS", "on")
    assert _search(client) == []  # the search refuses it at once
    out = service.sweep_unlabelled(Caller(principal="@alice:alpha", site="alpha"))
    assert out["swept"] == [item["id"]]
    assert _classifications(library, item["id"]) == []  # and its chunks are gone


def test_mvp_06_a1_retrieval_never_returns_an_item_above_the_persons_clearance(client, library):
    from axiom.rag.retriever import access_context_for_principal, retrieve

    item = _up(client, "pump-notes.md", DOC, ec_label="regulated").json()["items"][0]
    assert item["index_status"] == "indexed"
    assert set(_classifications(library, item["id"])) == {"regulated"}

    # Library search, at the default clearance (internal): withheld.
    assert _search(client) == []
    # Chat's retriever, same person: withheld.
    store = library["open_rag"]()
    chunks = retrieve(store, "zebracorn", None, corpora=["rag-org"],
                      access_context=access_context_for_principal("@alice:alpha"))
    assert [c for c in chunks if c.source_path == _source_path(library, item["id"])] == []
    # Reading it directly: refused above clearance.
    assert client.get(f"{API}/items/{item['id']}/content", headers=ALICE).status_code == 403

    # A person whose clearance attribute is regulated gets it back, on both paths.
    gc.set_clearance_resolver(lambda p: "regulated" if p == "@alice:alpha" else None)
    assert _search(client) == [item["id"]]
    chunks = retrieve(store, "zebracorn", None, corpora=["rag-org"],
                      access_context=access_context_for_principal("@alice:alpha"))
    assert chunks and chunks[0].classification == "regulated"
    # ...and a colleague without it still does not.
    assert _search(client, headers=ALICE_COLLEAGUE) == []


def test_mvp_16_a1_an_upload_is_answerable_in_chat_within_one_ingest_cycle_with_a_citation(client, library):
    """One ingest cycle is the upload's own pipeline run: when the upload returns, chat can cite it."""
    from axiom.rag.retriever import access_context_for_principal, retrieve

    item = _up(client, "pump-notes.md", DOC, ec_label="internal").json()["items"][0]
    store = library["open_rag"]()
    # A colleague at the site asks in chat; the served conversation reads the site corpus.
    chunks = retrieve(store, "zebracorn inspection", None, corpora=["rag-org"],
                      access_context=access_context_for_principal("@bob:alpha"))
    assert chunks, "the upload was not answerable"
    first = chunks[0]
    assert first.citation_key == "C1"
    assert first.source_path == _source_path(library, item["id"]) and "zebracorn" in first.chunk_text
    # No principal at all is the fail-closed floor: an internal item is not served anonymously.
    assert retrieve(store, "zebracorn", None, corpora=["rag-org"],
                    access_context=access_context_for_principal(None)) == []


# ------------------------------------------------------------------ retention


def _backdate(library, item_id, days):
    with library["factory"]() as s:
        row = s.get(LibraryItem, item_id)
        row.created_at = datetime.now(UTC) - timedelta(days=days)
        s.commit()


def _chain():
    from axiom.infra.audit_trail import audit_path, read_chain
    from axiom.infra.paths import get_user_state_dir

    return [r for r in read_chain(audit_path(get_user_state_dir())) if r["skill"].startswith("retention.delete")]


def test_mvp_04_a1_every_item_carries_its_label_and_its_retention_class(client, library):
    item = _up(client, "pump-notes.md", DOC, ec_label="internal").json()["items"][0]
    listed = client.get(f"{API}/items", headers=ALICE).json()
    row = (listed["items"] if isinstance(listed, dict) else listed)[0]
    for shown in (item, row):
        assert shown["ec_label"]["value"] == "internal"
        assert shown["retention"]["class"] == "operational" and shown["retention"]["floor"] == "5 years"
        assert shown["retention"]["deletable_from"] and shown["sample"] is False


def test_mvp_22_a2_deleting_forever_before_the_floor_is_refused_and_after_it_is_recorded(client, library):
    item = _up(client, "pump-notes.md", DOC, ec_label="internal").json()["items"][0]
    before = len(_chain())
    # Moving it to Deleted files keeps the bytes, and is recorded.
    assert client.delete(f"{API}/items/{item['id']}", headers=ALICE).status_code == 200
    assert [r["outcome"] for r in _chain()[before:]] == ["deleted"]

    # Delete forever is the one purge (operations.py): 423, in the floor's words.
    r = client.post(f"{API}/items/{item['id']}/purge", headers=ALICE)
    assert r.status_code == 423
    assert "operational" in r.json()["detail"] and "5 years" in r.json()["detail"]
    blob = library["root"] / "alpha" / "_content" / item["sha256"][:2] / item["sha256"]
    assert blob.exists()

    _backdate(library, item["id"], 6 * 365)
    r = client.post(f"{API}/items/{item['id']}/purge", headers=ALICE)
    assert r.status_code == 200, r.text
    assert r.json()["purged"] is True and r.json()["bytes_removed"] == 1
    assert not blob.exists()
    # The soft delete is in the retention chain; the purge's record is the
    # library's purge event (and the action ledger, test_library_operations).
    assert [r["outcome"] for r in _chain()[before:]] == ["deleted"]
    with library["factory"]() as s:
        events = [e.action for e in s.query(LibraryEvent).filter_by(item_id=item["id"]).order_by(LibraryEvent.id)]
        assert events[-3:] == ["delete", "purge-refused", "purge"]
    # It is gone from Deleted files and cannot come back.
    assert client.post(f"{API}/items/{item['id']}/restore", headers=ALICE).status_code == 404
    deleted = client.get(f"{API}/items", params={"deleted": True}, headers=ALICE).json()
    assert item["id"] not in [d["id"] for d in (deleted["items"] if isinstance(deleted, dict) else deleted)]


def test_mvp_22_a2_holds_keep_an_item_however_old_it_is(client, library):
    item = _up(client, "pump-notes.md", DOC, ec_label="internal", retention_class="system").json()["items"][0]
    client.delete(f"{API}/items/{item['id']}", headers=ALICE)
    _backdate(library, item["id"], 50 * 365)
    with library["factory"]() as s:
        s.get(LibraryItem, item["id"]).ec = {"verdict": "held", "reasons": ["an input to a controlled code"]}
        s.commit()
    r = client.post(f"{API}/items/{item['id']}/purge", headers=ALICE)
    assert r.status_code == 423 and "export-control hold" in r.json()["detail"]

    with library["factory"]() as s:
        row = s.get(LibraryItem, item["id"])
        row.ec, row.retention_class = {}, "legal_hold"
        s.commit()
    r = client.post(f"{API}/items/{item['id']}/purge", headers=ALICE)
    assert r.status_code == 423 and "legal hold" in r.json()["detail"]


def test_a_retention_class_may_lengthen_never_shorten(client, library):
    item = _up(client, "pump-notes.md", DOC, ec_label="internal").json()["items"][0]
    r = client.post(f"{API}/items/{item['id']}/label", json={"retention_class": "system"}, headers=ALICE)
    assert r.status_code == 403 and "only lengthen" in r.json()["detail"]
    r = client.post(f"{API}/items/{item['id']}/label", json={"retention_class": "legal_hold"}, headers=ALICE)
    assert r.status_code == 200 and r.json()["retention"]["class"] == "legal_hold"
    r = client.post(f"{API}/items/{item['id']}/label", json={"retention_class": "operational"}, headers=ALICE)
    assert r.status_code == 403 and "released" in r.json()["detail"]
    r = client.post(f"{API}/items/{item['id']}/label",
                    json={"retention_class": "operational", "release_hold": True}, headers=ALICE)
    assert r.status_code == 200 and r.json()["retention"]["class"] == "operational"


# ------------------------------------------------------------------ sample items


def test_mvp_25_a5_a_sample_item_is_labelled_sample_and_never_retrieved_or_exported(client, library):
    item = _up(client, "pump-notes.md", DOC, ec_label="internal", sample="true").json()["items"][0]
    assert item["sample"] is True and item["index_status"] == "sample"
    assert _classifications(library, item["id"]) == []
    assert _search(client) == []
    r = client.get(f"{API}/items/{item['id']}/content", params={"download": True}, headers=ALICE)
    assert r.status_code == 403 and "sample" in r.json()["detail"]
    # It still lists, as a sample, so a first run shows something.
    listed = client.get(f"{API}/items", headers=ALICE).json()
    rows = listed["items"] if isinstance(listed, dict) else listed
    assert [x["sample"] for x in rows if x["id"] == item["id"]] == [True]


# ------------------------------------------------------------------ the other surfaces


def test_mvp_22_label_is_one_skill_on_cli_mcp_and_chat_and_purge_is_cli_only(library, tmp_path):
    """Parity: "what's this file's label" (MCP / chat), confirming it, and the sweep, through one skill."""
    import logging

    from axiom.extensions.builtins.library.skills import SPECS, bind_default
    from axiom.infra.principal import PrincipalContext
    from axiom.infra.skills import SkillContext

    reg = bind_default()
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"),
                       principal=PrincipalContext(handle="@carol:alpha"), surface="mcp")
    src = tmp_path / "notes.md"
    src.write_bytes(DOC)
    up = reg.invoke("library.upload", {"path": str(src)}, ctx)
    assert up.ok and up.value["index_status"] == "awaiting-label"
    asked = reg.invoke("library.label", {"id": up.value["id"]}, ctx)
    assert asked.ok and asked.value["ec_label"]["value"] == "internal" and not asked.value["ec_label"]["confirmed"]
    done = reg.invoke("library.label", {"id": up.value["id"], "label": "internal"}, ctx)
    assert done.ok and done.value["index_status"] == "indexed"
    swept = reg.invoke("library.label", {"sweep": True}, ctx)
    assert swept.ok and swept.value["count"] == 0

    surfaces = {s.name: set(s.surfaces) for s in SPECS}
    assert {"cli", "mcp", "agent_tool"} <= surfaces["library.label"]
    assert surfaces["library.purge"] == {"cli"}


def test_the_cli_confirms_a_label_and_refuses_a_purge_in_the_floors_words(library, tmp_path, capsys, monkeypatch):
    from axiom.extensions.builtins.library import cli

    monkeypatch.setattr("axiom.infra.principal.local_handle", lambda: "@carol:alpha", raising=False)
    src = tmp_path / "notes.md"
    src.write_bytes(DOC)
    assert cli.main(["upload", str(src), "--label", "internal", "--json"]) == 0
    import json as _json

    item = _json.loads(capsys.readouterr().out)[0]
    assert item["ec_label"]["confirmed"] is True
    assert cli.main(["label", item["id"]]) == 0
    assert "label internal (confirmed); retention operational (kept at least 5 years)" in capsys.readouterr().out
    assert cli.main(["rm", item["id"]]) == 0
    capsys.readouterr()
    assert cli.main(["purge", item["id"]]) == 2
    assert "5 years" in capsys.readouterr().err
