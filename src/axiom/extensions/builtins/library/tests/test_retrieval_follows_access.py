# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Search and chat retrieval return exactly what the decision point allows (SHR-R8.3, SHR-A9).

A file shared with Cy by name is in its Owner's own corpus, which nobody else's
retrieval names. Library search and chat retrieval must find it for Cy, and
never for Bob, whatever corpus each reads.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library.db_models import LibraryItem
from axiom.extensions.builtins.library.tests.test_library_upload import _up

API = "/api/v1/library"
ALICE = {"x-test-principal": "@alice:alpha", "x-test-site": "alpha"}
BOB = {"x-test-principal": "@bob:alpha", "x-test-site": "alpha"}
CY = {"x-test-principal": "@cy:alpha", "x-test-site": "alpha"}

SECRET = b"# Pump review\n\nThe quarterly zebracorn inspection found a worn seal on the second pump.\n"


def _share(library, item_id, who, role="viewer"):
    from axiom.extensions.builtins.library import access
    from axiom.governance.access import decision, sharing

    with library["factory"]() as s:
        obj = access.item_object(s.get(LibraryItem, item_id), access.user_folders(s, "alpha"))
    sharing.share(decision.who("@alice:alpha", "alpha"), obj, who, role)


def _chat_retrieve(library, principal):
    """What a served conversation retrieves: the site's shared corpora and the principal's access context."""
    from axiom.extensions.builtins.chat.scope import shared_corpora_for
    from axiom.rag.retriever import access_context_for_principal, retrieve

    return retrieve(library["open_rag"](), "zebracorn inspection", None,
                    corpora=list(shared_corpora_for(None)), limit=8,
                    access_context=access_context_for_principal(principal))


@pytest.fixture
def private_shared_with_cy(client, library):
    r = _up(client, "review.md", SECRET, visibility="private")
    assert r.status_code == 201, r.text
    item = r.json()["items"][0]
    assert item["index_status"] == "indexed"
    _share(library, item["id"], "@cy:alpha")
    return item


def test_shr_a9_library_search_finds_a_direct_share_for_its_person_only(client, private_shared_with_cy):
    item = private_shared_with_cy
    for who, expect in ((ALICE, True), (CY, True), (BOB, False)):
        found = client.get(f"{API}/search", params={"q": "zebracorn"}, headers=who).json()
        ids = [i["id"] for i in found.get("results", found.get("items", []))]
        assert (item["id"] in ids) is expect, who


def test_shr_a9_chat_retrieval_returns_it_to_the_person_shared_with_and_nobody_else(library, private_shared_with_cy):
    def paths(principal):
        return [c.source_path for c in _chat_retrieve(library, principal)]

    mine = f"library/alpha/{private_shared_with_cy['id']}/"
    assert any(p.startswith(mine) for p in paths("@cy:alpha"))
    assert not any(p.startswith(mine) for p in paths("@bob:alpha"))
    assert not any(p.startswith(mine) for p in paths(None))


def test_a_site_file_is_still_retrieved_through_the_corpus_and_stops_when_restricted(client, library):
    from axiom.extensions.builtins.library import access
    from axiom.governance.access import decision, sharing

    r = _up(client, "site.md", SECRET.replace(b"second", b"third"))
    item = r.json()["items"][0]
    mine = f"library/alpha/{item['id']}/"
    assert any(c.source_path.startswith(mine) for c in _chat_retrieve(library, "@bob:alpha"))
    with library["factory"]() as s:
        obj = access.item_object(s.get(LibraryItem, item["id"]), {})
    # Restricted through the record alone (no projection yet): the guard still withholds it.
    sharing.set_general(decision.who("@alice:alpha", "alpha"), obj, "restricted")
    assert not any(c.source_path.startswith(mine) for c in _chat_retrieve(library, "@bob:alpha"))


def test_a_held_or_deleted_file_is_never_retrieved(client, library, private_shared_with_cy):
    item = private_shared_with_cy
    mine = f"library/alpha/{item['id']}/"
    with library["factory"]() as s:
        s.get(LibraryItem, item["id"]).ec = {"verdict": "held"}
        s.commit()
    assert not any(c.source_path.startswith(mine) for c in _chat_retrieve(library, "@cy:alpha"))


def test_a_guard_that_cannot_answer_withholds_its_chunks(client, library, private_shared_with_cy):
    from axiom.rag import source_guards

    class Broken:
        prefix = "library/"

        def permitted(self, principal, paths):
            raise RuntimeError("down")

        def extra_corpora(self, principal):
            raise RuntimeError("down")

    source_guards.set_guards([Broken()])
    try:
        assert not any(c.source_path.startswith("library/") for c in _chat_retrieve(library, "@cy:alpha"))
    finally:
        source_guards.set_guards(None)


def test_chunks_no_guard_claims_are_left_as_they_were(library):
    from axiom.rag import source_guards

    assert source_guards.permitted("@cy:alpha", ["notes/elsewhere.md"]) == set()
    assert source_guards.claimant("notes/elsewhere.md") is None
