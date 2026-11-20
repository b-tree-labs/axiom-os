# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-213 D9: releasing a held file. Who may, what it covers, and the trail it leaves.

The catalog and retrieval store are SQLite and bronze is a directory (the
fixtures of ``test_library_upload``); decisions are sealed with a throwaway key.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library import contrib, ec_review
from axiom.extensions.builtins.library.extract import registry
from axiom.extensions.builtins.library.extract.contract import ExtractionResult, TextSection
from axiom.extensions.builtins.library.tests.test_library_upload import ALICE, ALICE_COLLEAGUE, _up
from axiom.extensions.builtins.library.tests.test_upload_pipeline import API, _m
from axiom.extensions.builtins.library.tests.test_upload_pipeline import _pipeline as _pipeline  # noqa: F401

BOB = ALICE_COLLEAGUE
CAROL = {"x-test-principal": "@carol:alpha", "x-test-site": "alpha"}


@pytest.fixture
def held_deck(client, library, monkeypatch):
    """A controlled code, an extractor for its decks, and Bob as the site's ec-reviewer."""
    monkeypatch.setenv(ec_review.REVIEWERS_ENV, "@bob:alpha,@alice:alpha")
    contrib.add("controlled_codes", {"code": "widgetsim", "basis": "declared for this test"})

    def deck(ctx):
        text = ctx.path.read_text()
        return ExtractionResult(text=[TextSection(text)], code="widgetsim", features={"kind": "input"})

    registry.register(
        _m("pack.extract.deck", outputs=["text"], tags=["rag", "input-deck"], ec="payload",
           supersedes=["core.extract.text"], accepts=[{"extension": ".wid", "sniff": "text"}]),
        deck,
    )
    item = _up(client, "core.wid", b"cells and the zebrafinch method\n").json()["items"][0]
    assert item["ec"]["verdict"] == "held"
    return item


def _decide(client, item_id, verb, reason, headers):
    return client.post(f"{API}/items/{item_id}/ec/{verb}", json={"reason": reason}, headers=headers)


def _search(client, q):
    return client.get(f"{API}/search?q={q}", headers=ALICE).json()["results"]


def test_a_held_item_says_why_with_its_code_and_kind_of_file(client, held_deck):
    state = client.get(f"{API}/items/{held_deck['id']}/ec", headers=BOB).json()
    assert state["verdict"] == "held" and state["code"] == "widgetsim" and state["kind"] == "input"
    assert "an input to widgetsim" in state["reasons"][0]
    assert state["can_release"] is True and state["why_not"] == ""
    # The automatic hold is the first record of the trail.
    assert [h["action"] for h in state["history"]] == ["hold"]
    assert state["history"][0]["signed"] is True


def test_the_uploader_can_never_release_their_own_file_even_as_a_reviewer(client, held_deck):
    state = client.get(f"{API}/items/{held_deck['id']}/ec", headers=ALICE).json()
    assert state["can_release"] is False and "uploaded" in state["why_not"]
    r = _decide(client, held_deck["id"], "release", "it is mine and fine", ALICE)
    assert r.status_code == 403 and "uploaded" in r.json()["detail"]
    assert _search(client, "zebrafinch") == []


def test_someone_without_the_role_is_refused(client, held_deck):
    r = _decide(client, held_deck["id"], "release", "looks fine", CAROL)
    assert r.status_code == 403 and "ec-reviewer" in r.json()["detail"]


def test_a_decision_needs_a_reason(client, held_deck):
    assert _decide(client, held_deck["id"], "release", "   ", BOB).status_code == 422
    assert _decide(client, held_deck["id"], "keep-held", "", BOB).status_code == 422


def test_a_reviewer_releases_with_a_reason_and_the_file_becomes_searchable(client, held_deck, library):
    r = _decide(client, held_deck["id"], "release", "reviewed with the export-control office: public benchmark", BOB)
    assert r.status_code == 200, r.text
    state = r.json()
    assert state["verdict"] == "released"
    assert [(h["action"], h["principal"]) for h in state["history"]] == [
        ("hold", ec_review.SYSTEM), ("release", "@bob:alpha")]
    assert state["history"][1]["reason"].startswith("reviewed with")
    item = client.get(f"{API}/items/{held_deck['id']}", headers=ALICE).json()
    assert item["index_status"] == "indexed" and item["ec"]["release"]["principal"] == "@bob:alpha"
    assert [h["id"] for h in _search(client, "zebrafinch")] == [held_deck["id"]]
    # The item's own history shows the decisions as events.
    events = client.get(f"{API}/items/{held_deck['id']}/pipeline", headers=ALICE).json()["events"]
    assert {"ec-hold", "ec-release"} <= {e["action"] for e in events}


def test_the_trail_is_a_sealed_chain_the_standalone_verifier_accepts(client, held_deck, library):
    from axiom.attest.chain import verify_chain
    from axiom.extensions.builtins.library.db_models import LibraryEcReview

    _decide(client, held_deck["id"], "keep-held", "needs the code owner's word first", BOB)
    _decide(client, held_deck["id"], "release", "code owner confirmed it is a public case", BOB)
    with library["factory"]() as s:
        records = [r.record for r in s.query(LibraryEcReview).order_by(LibraryEcReview.seq).all()]
    assert [r["action"] for r in records] == ["hold", "keep-held", "release"]
    signer = library["signer"]
    report = verify_chain(records, {signer.key_id: signer.public_bytes})
    assert report.ok, report


def test_keep_held_is_recorded_and_changes_nothing(client, held_deck):
    r = _decide(client, held_deck["id"], "keep-held", "unclear provenance", BOB)
    assert r.status_code == 200 and r.json()["verdict"] == "held"
    assert [h["action"] for h in r.json()["history"]] == ["hold", "keep-held"]
    assert _search(client, "zebrafinch") == []


def test_a_release_can_be_taken_back(client, held_deck):
    _decide(client, held_deck["id"], "release", "public case", BOB)
    assert _search(client, "zebrafinch")
    r = _decide(client, held_deck["id"], "re-hold", "the office asked to hold it after all", ALICE)
    assert r.status_code == 200 and r.json()["verdict"] == "held"
    assert _search(client, "zebrafinch") == []
    item = client.get(f"{API}/items/{held_deck['id']}", headers=ALICE).json()
    assert item["index_status"] == "held"
    assert [h["action"] for h in r.json()["history"]] == ["hold", "release", "re-hold"]


def test_a_release_covers_one_version_and_a_new_version_is_held_again(client, held_deck):
    _decide(client, held_deck["id"], "release", "public case", BOB)
    v2 = _up(client, "core.wid", b"cells and the zebrafinch method, revised with the koala pins\n").json()["items"][0]
    assert v2["id"] != held_deck["id"]
    assert v2["ec"]["verdict"] == "held" and v2["index_status"] == "held"
    assert any("earlier version" in r for r in v2["ec"]["reasons"])
    assert _search(client, "koala") == []
    state = client.get(f"{API}/items/{v2['id']}/ec", headers=BOB).json()
    assert [h["action"] for h in state["history"]] == ["hold"]


def test_a_re_extraction_keeps_a_release_for_the_same_basis(client, held_deck):
    _decide(client, held_deck["id"], "release", "public case", BOB)
    client.post(f"{API}/items/{held_deck['id']}/reextract", json={}, headers=ALICE)
    item = client.get(f"{API}/items/{held_deck['id']}", headers=ALICE).json()
    assert item["ec"]["verdict"] == "released" and item["index_status"] == "indexed"


def test_a_new_reason_to_hold_re_holds_a_released_file(client, held_deck):
    _decide(client, held_deck["id"], "release", "public case", BOB)
    contrib.reset(rediscover=False)
    contrib.add("controlled_codes", {"code": "widgetsim", "basis": "reclassified by the office"})
    client.post(f"{API}/items/{held_deck['id']}/reextract", json={}, headers=ALICE)
    item = client.get(f"{API}/items/{held_deck['id']}", headers=ALICE).json()
    assert item["ec"]["verdict"] == "held" and item["index_status"] == "held"
    state = client.get(f"{API}/items/{held_deck['id']}/ec", headers=BOB).json()
    assert [h["action"] for h in state["history"]] == ["hold", "release", "re-hold"]
    assert state["history"][-1]["principal"] == ec_review.SYSTEM


def test_a_release_that_cannot_be_attested_is_refused(client, held_deck):
    from axiom.extensions.builtins.attest import signing

    def broken():
        raise RuntimeError("no node key")

    signing.set_provider(broken, keys=lambda: {})
    r = _decide(client, held_deck["id"], "release", "public case", BOB)
    assert r.status_code == 403 and "attested" in r.json()["detail"]
    item = client.get(f"{API}/items/{held_deck['id']}", headers=ALICE).json()
    assert item["ec"]["verdict"] == "held"


def test_with_no_reviewer_named_the_owner_is_the_default_reviewer(client, held_deck, monkeypatch):
    monkeypatch.delenv(ec_review.REVIEWERS_ENV)
    monkeypatch.setattr(ec_review, "owners", lambda: ["carol"])
    assert ec_review.reviewers("alpha")["default_owner"] is True
    assert _decide(client, held_deck["id"], "release", "looks fine", BOB).status_code == 403
    r = _decide(client, held_deck["id"], "release", "owner's review: public case", CAROL)
    assert r.status_code == 200, r.text


def test_the_site_role_store_names_reviewers(client, held_deck, monkeypatch, tmp_path):
    from axiom.extensions.builtins.attest import roles

    monkeypatch.delenv(ec_review.REVIEWERS_ENV)
    roles.grant("@carol:alpha", "alpha", ec_review.ROLE_ID, by="@admin:alpha", state_dir=tmp_path / "state")
    assert ec_review.is_reviewer("@carol:alpha", "alpha")
    assert not ec_review.is_reviewer("@bob:alpha", "alpha")
    assert _decide(client, held_deck["id"], "release", "role holder's review", CAROL).status_code == 200


def test_a_stub_that_names_its_code_holds_and_files_before_a_reader_exists(client, library):
    from axiom.extensions.builtins.library.extract.contract import NotYet

    contrib.add("controlled_codes", {"code": "widgetsim"})

    def stub(ctx):
        raise NotYet("recognised, not read yet")

    registry.register(
        _m("pack.extract.widget_stub", outputs=["metadata"], tags=["input-deck"], ec="payload", status="stub",
           code="widgetsim", supersedes=["core.extract.text"], accepts=[{"extension": ".wid", "sniff": "text"}]),
        stub,
    )
    item = _up(client, "plant.wid", b"zebrafinch\n").json()["items"][0]
    assert item["ec"]["verdict"] == "held" and item["ec"]["code"] == "widgetsim"
    assert item["ec"]["extractor"] == "pack.extract.widget_stub"


def test_a_new_version_of_the_same_item_is_re_checked_and_its_release_stays_with_the_old_bytes(client, held_deck):
    from axiom.extensions.builtins.library import operations
    from axiom.extensions.builtins.library.scope import make_caller

    _decide(client, held_deck["id"], "release", "public case", BOB)
    alice = make_caller("@alice:alpha", "alpha")
    operations.add_version(alice, held_deck["id"], "core.wid", b"cells and the zebrafinch method, now with koala pins\n")
    item = client.get(f"{API}/items/{held_deck['id']}", headers=ALICE).json()
    assert item["ec"]["verdict"] == "held" and item["index_status"] == "held"
    assert any("earlier version" in r for r in item["ec"]["reasons"])
    assert _search(client, "koala") == []
    # Restoring the released bytes brings their release back with them.
    operations.restore_version(alice, held_deck["id"], 1)
    item = client.get(f"{API}/items/{held_deck['id']}", headers=ALICE).json()
    assert item["ec"]["verdict"] == "released"


@pytest.mark.acceptance("MVP-04-A1")
def test_a_held_file_reads_as_held_in_every_view(client, held_deck):
    """Starred, Recent and As uploaded carry the export-control verdict, as the item does."""
    item_id = held_deck["id"]
    client.put(f"{API}/items/{item_id}/star", headers=ALICE)
    starred = client.get(f"{API}/starred", headers=ALICE).json()["items"]
    recent = client.get(f"{API}/recent", headers=ALICE).json()["items"]
    originals = client.get(f"{API}/originals", headers=ALICE).json()["items"]
    for rows in (starred, recent):
        (row,) = [r for r in rows if r["id"] == item_id]
        assert row["ec"]["verdict"] == "held" and row["index_status"] == "held"
    (row,) = [r for r in originals if r["item_id"] == item_id]
    assert row["ec"]["verdict"] == "held"
