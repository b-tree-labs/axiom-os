# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""The rag reaper — and the corpora it must refuse to touch."""

from __future__ import annotations

import pytest

from axiom.infra.tenancy import ReaperRegistry, offboard
from axiom.rag.tenant_reaper import RagReaper

#: A realistic library: per-tenant uploads, plus topical corpora that several
#: facilities read and none of them own.
INVENTORY = {
    "acu-flowloop:uploads": 120,
    "acu-flowloop:reports": 40,
    "vcu-flowloop:uploads": 95,
    "netl-triga:docs": 300,
    "msr-literature": 5000,
    "reactor-general": 8000,
    "rag-community": 2200,
    "shared": 60,
}


class _Store:
    def __init__(self, inventory=None):
        self.inventory = dict(INVENTORY if inventory is None else inventory)
        self.deleted: list[str] = []

    def stats(self):
        return {"chunks_by_corpus": dict(self.inventory)}

    def delete_corpus(self, corpus: str) -> int:
        self.deleted.append(corpus)
        return self.inventory.pop(corpus, 0)


def test_finds_only_the_corpora_the_tenant_owns() -> None:
    found = list(RagReaper(_Store()).find("acu-flowloop"))
    assert [r.identifier for r in found] == ["acu-flowloop:reports", "acu-flowloop:uploads"]
    assert all(r.subsystem == "rag" and r.kind == "corpus" for r in found)


def test_reports_how_much_would_be_deleted() -> None:
    found = {r.identifier: r.detail for r in RagReaper(_Store()).find("acu-flowloop")}
    assert found["acu-flowloop:uploads"] == "120 chunk(s)"


@pytest.mark.parametrize(
    "library", ["msr-literature", "reactor-general", "rag-community", "shared"]
)
def test_a_shared_library_is_never_deleted(library) -> None:
    """The failure this reaper exists to make impossible. A flow loop reads far
    more molten-salt material than it owns; deleting it while removing one
    tenant would silently degrade every other tenant's retrieval, and nothing
    would report it — the corpus would simply stop being found."""
    store = _Store()
    RagReaper(store).reap("acu-flowloop")
    assert library not in store.deleted
    assert library in store.inventory


def test_another_tenants_corpus_is_never_deleted() -> None:
    store = _Store()
    RagReaper(store).reap("acu-flowloop")
    assert "vcu-flowloop:uploads" in store.inventory
    assert "netl-triga:docs" in store.inventory


def test_reap_removes_every_owned_corpus_and_says_how_many() -> None:
    store = _Store()
    lines = list(RagReaper(store).reap("acu-flowloop"))
    assert sorted(store.deleted) == ["acu-flowloop:reports", "acu-flowloop:uploads"]
    assert any("120 chunk(s)" in line for line in lines)


def test_a_tenant_with_no_corpora_deletes_nothing() -> None:
    store = _Store()
    assert list(RagReaper(store).reap("tamu-flowloop")) == []
    assert store.deleted == []


def test_a_prefix_collision_is_not_ownership() -> None:
    """`acu-flowloop-archive:docs` belongs to a different tenant than
    `acu-flowloop`, and a naive prefix match would delete it."""
    store = _Store({"acu-flowloop:uploads": 5, "acu-flowloop-archive:docs": 9})
    RagReaper(store).reap("acu-flowloop")
    assert store.deleted == ["acu-flowloop:uploads"]
    assert "acu-flowloop-archive:docs" in store.inventory


def test_offboard_now_reports_rag_and_can_drop_it_from_unreaped() -> None:
    """Registered, the reaper turns rag from an admission into a deletion."""
    store = _Store()
    registry = ReaperRegistry()
    registry.register(RagReaper(store))
    report = offboard("acu-flowloop", dry_run=False, registry=registry, unreaped=("caches",))
    assert sorted(store.deleted) == ["acu-flowloop:reports", "acu-flowloop:uploads"]
    assert "rag" not in report.unreaped
    assert report.complete is False  # caches remain, and the report still says so


def test_dry_run_deletes_nothing(tmp_path) -> None:
    store = _Store()
    registry = ReaperRegistry()
    registry.register(RagReaper(store))
    report = offboard("acu-flowloop", registry=registry)
    assert store.deleted == []
    assert len(report.found) == 2
