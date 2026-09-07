# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Retrieval's tenant dimension (ADR-025 §1, §3).

Two mechanisms, deliberately: the corpus filter keeps a tenant's candidates
from being crowded out, and the access filter is the belt that holds if a
corpus name is ever constructed wrong. These tests pin the second and the
interaction, because the first is only a quality property until the second
makes it a safety one.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from axiom.rag.retriever import (
    SHARED_SITE,
    AccessContext,
    retrieve,
    tenant_corpora,
)


@dataclass
class _Hit:
    source_path: str
    source_title: str = "t"
    chunk_text: str = "body"
    chunk_index: int = 0
    corpus: str = "c"
    similarity: float = 0.9


class _Store:
    """Returns whatever it was given, so the filter is what is under test."""

    def __init__(self, paths):
        self._hits = [_Hit(source_path=p) for p in paths]
        self.corpora_seen: list[list[str] | None] = []

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **kw):
        self.corpora_seen.append(corpora)
        return list(self._hits[:limit])


def _sites(mapping):
    return lambda path: mapping.get(path)


def _retrieve(store, ctx, sites, **kw):
    return retrieve(
        store,
        "q",
        None,
        access_context=ctx,
        site_lookup=_sites(sites),
        limit=10,
        **kw,
    )


ANY_TIER = dict(max_access_tier="restricted", allowed_classifications=frozenset({"unclassified"}))


# ------------------------------------------------------------------ isolation


def test_a_tenant_sees_only_its_own_chunks() -> None:
    store = _Store(["a.md", "b.md"])
    ctx = AccessContext(site="acu-flowloop", **ANY_TIER)
    out = _retrieve(store, ctx, {"a.md": "acu-flowloop", "b.md": "vcu-flowloop"})
    assert [c.source_path for c in out] == ["a.md"]
    assert out[0].site == "acu-flowloop"


def test_an_unattributed_chunk_is_denied_to_a_tenant() -> None:
    """Absence must not read as 'everyone's', or every chunk indexed before
    tenancy existed becomes visible to every tenant."""
    store = _Store(["legacy.md"])
    ctx = AccessContext(site="acu-flowloop", **ANY_TIER)
    assert _retrieve(store, ctx, {}) == []


def test_explicitly_shared_chunks_are_visible_to_every_tenant() -> None:
    store = _Store(["platform-docs.md"])
    for site in ("acu-flowloop", "vcu-flowloop"):
        ctx = AccessContext(site=site, **ANY_TIER)
        out = _retrieve(store, ctx, {"platform-docs.md": SHARED_SITE})
        assert [c.source_path for c in out] == ["platform-docs.md"]


def test_an_unconstrained_context_is_unchanged() -> None:
    """The administrative path, and every caller that predates tenancy."""
    store = _Store(["a.md", "b.md"])
    ctx = AccessContext(site=None, **ANY_TIER)
    out = _retrieve(store, ctx, {"a.md": "acu-flowloop", "b.md": None})
    assert len(out) == 2


def test_no_access_context_at_all_still_applies_no_filter() -> None:
    store = _Store(["a.md"])
    out = retrieve(store, "q", None, site_lookup=_sites({"a.md": "vcu-flowloop"}), limit=5)
    assert len(out) == 1


def test_site_is_checked_alongside_tier_not_instead_of_it() -> None:
    """A tenant's own chunk must still clear classification and tier."""
    store = _Store(["secret.md"])
    ctx = AccessContext(
        site="acu-flowloop",
        max_access_tier="public",
        allowed_classifications=frozenset({"unclassified"}),
    )
    out = retrieve(
        store,
        "q",
        None,
        access_context=ctx,
        site_lookup=_sites({"secret.md": "acu-flowloop"}),
        classification_lookup=lambda p: "restricted",
        limit=5,
    )
    assert out == []


# ------------------------------------------------------------- crowding out


def test_the_corpus_filter_is_what_stops_crowding_out() -> None:
    """The access filter runs AFTER fusion. Without the corpus push-down, a
    noisy neighbour fills the candidate window and the tenant gets an answer
    that is correct and nearly empty, with no indication why."""
    store = _Store([f"other-{i}.md" for i in range(8)] + ["mine.md"])
    ctx = AccessContext(site="acu-flowloop", **ANY_TIER)
    sites = {f"other-{i}.md": "vcu-flowloop" for i in range(8)}
    sites["mine.md"] = "acu-flowloop"
    out = retrieve(
        store,
        "q",
        None,
        access_context=ctx,
        site_lookup=_sites(sites),
        limit=5,
        retrieval_breadth=8,
    )
    assert out == []  # the tenant's own chunk never reached the window
    # with the push-down the store is asked only for corpora the tenant may read
    assert tenant_corpora("acu-flowloop", ["acu-flowloop:uploads", "vcu-flowloop:uploads"]) == [
        "acu-flowloop:uploads"
    ]


# ------------------------------------------------------------ corpus mapping


def test_tenant_corpora_returns_no_filter_when_unconstrained() -> None:
    assert tenant_corpora(None) is None


def test_tenant_corpora_includes_the_shared_corpus_when_present() -> None:
    got = tenant_corpora("acu-flowloop", ["acu-flowloop:uploads", "shared", "vcu-flowloop:docs"])
    assert set(got) == {"acu-flowloop:uploads", "shared"}


def test_tenant_corpora_omits_shared_when_the_catalog_lacks_it() -> None:
    assert tenant_corpora("acu-flowloop", ["acu-flowloop:uploads"]) == ["acu-flowloop:uploads"]


def test_tenant_corpora_never_returns_another_tenants_corpus() -> None:
    for got in (
        tenant_corpora("acu", ["acu:uploads", "acu-other:uploads", "vcu:uploads"]),
        tenant_corpora("acu", ["vcu:uploads"]),
    ):
        assert not any(c.startswith("vcu") or c.startswith("acu-other") for c in got)


@pytest.mark.parametrize("site", ["acu-flowloop", "vcu-flowloop", "netl-triga"])
def test_every_tenant_gets_a_disjoint_view(site) -> None:
    catalog = ["acu-flowloop:uploads", "vcu-flowloop:uploads", "netl-triga:docs", "shared"]
    got = set(tenant_corpora(site, catalog))
    others = {c for c in catalog if not c.startswith(f"{site}:") and c != SHARED_SITE}
    assert got & others == set()
