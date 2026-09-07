# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""End-to-end tenancy over a realistic multi-facility corpus.

The unit tests pin each rule; these exercise the mechanisms together at a
scale where the interesting failures live — a corpus with four tenants of
very unequal size, queried by every tenant, with each protection defeated in
turn to prove the other still holds.

Derived from a run against 34 real partner documents out of Box (ACU, VCU,
TAMU and NETL), reduced here to a fixture so it runs anywhere. The size
imbalance is kept because it is what makes the crowding-out case real: NETL
had ~4× the chunks of ACU, and a post-fusion filter alone hands the smaller
tenant an answer that is correct and nearly empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from axiom.rag.retriever import (
    SHARED_SITE,
    AccessContext,
    retrieve,
    tenant_corpora,
)

TIERS = dict(max_access_tier="restricted", allowed_classifications=frozenset({"unclassified"}))
QUERY = "thermocouple temperature measurement data"

#: (site, n_chunks) — the real corpus's shape, including its imbalance.
FACILITIES = (
    ("acu-flowloop", 27),
    ("vcu-flowloop", 24),
    ("tamu-flowloop", 55),
    ("netl-triga", 100),
)


@dataclass
class _Hit:
    source_path: str
    source_title: str
    chunk_text: str
    chunk_index: int
    corpus: str
    similarity: float


def _corpus():
    chunks = []
    for site, count in FACILITIES:
        for i in range(count):
            chunks.append(
                {
                    "site": site,
                    "path": f"/{site}/doc{i}.md",
                    "title": f"{site} doc {i}",
                    "chunk_index": i,
                    "corpus": f"{site}:docs",
                    # The big tenant's chunks genuinely rank higher, as they did
                    # in the real corpus. Uniform scores would make ordering
                    # arbitrary and the crowding-out case meaningless.
                    "chunk_text": (
                        f"{site} report {i}: "
                        + "thermocouple temperature measurement data " * (3 if count > 50 else 1)
                        + "recorded during the test."
                    ),
                }
            )
    return chunks


class _Store:
    """Keyword-scored, and honours ``corpora`` — so the push-down is real."""

    def __init__(self, chunks):
        self.chunks = chunks

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **kw):
        terms = [t for t in re.findall(r"\w+", query_text.lower()) if len(t) > 3]
        scored = []
        for c in self.chunks:
            if corpora is not None and c["corpus"] not in corpora:
                continue
            score = sum(c["chunk_text"].lower().count(t) for t in terms)
            if score:
                scored.append((score, c))
        scored.sort(key=lambda s: -s[0])
        return [
            _Hit(c["path"], c["title"], c["chunk_text"], c["chunk_index"], c["corpus"], float(s))
            for s, c in scored[:limit]
        ]


@pytest.fixture
def world():
    chunks = _corpus()
    shared = {
        "site": SHARED_SITE,
        "path": "/shared/glossary.md",
        "title": "glossary",
        "chunk_index": 0,
        "corpus": SHARED_SITE,
        "chunk_text": "Thermocouple: a temperature measurement data device, platform-wide.",
    }
    store = _Store([*chunks, shared])
    sites = {c["path"]: c["site"] for c in chunks}
    sites[shared["path"]] = SHARED_SITE
    catalog = sorted({c["corpus"] for c in chunks} | {SHARED_SITE})
    return store, (lambda p: sites.get(p)), catalog


def _ask(world, site, *, corpora=True, context=True, breadth=40, limit=10):
    store, lookup, catalog = world
    return retrieve(
        store,
        QUERY,
        None,
        corpora=tenant_corpora(site, catalog) if corpora else None,
        access_context=AccessContext(site=site, **TIERS) if context else None,
        site_lookup=lookup,
        limit=limit,
        retrieval_breadth=breadth,
    )


@pytest.mark.parametrize("site", [s for s, _ in FACILITIES])
def test_no_tenant_ever_sees_another(world, site) -> None:
    out = _ask(world, site)
    assert out, f"{site} got no results at all"
    assert {c.site for c in out} <= {site, SHARED_SITE}


def test_the_access_filter_holds_when_the_corpus_filter_is_bypassed(world) -> None:
    """Braces defeated — a corpus name constructed wrong, or a caller that
    forgets the push-down entirely.

    The safety property is that nothing leaks. The *answer* may well be empty,
    and that is the point of the next test rather than a flaw in this one.
    """
    out = _ask(world, "acu-flowloop", corpora=False)
    assert {c.site for c in out} <= {"acu-flowloop", SHARED_SITE}


def test_without_the_push_down_a_small_tenant_can_get_nothing_at_all(world) -> None:
    """The severity of crowding out, stated plainly: the largest tenant's
    chunks fill the candidate window, the post-fusion filter removes every one
    of them, and the smaller tenant receives an empty answer that is perfectly
    correct and completely useless. Nothing in the response says why — which is
    what makes this a safety mechanism that must not be relied on alone."""
    starved = _ask(world, "acu-flowloop", corpora=False)
    healthy = _ask(world, "acu-flowloop")
    assert starved == []
    assert len(healthy) >= 10


def test_the_corpus_filter_holds_when_the_access_context_is_missing(world) -> None:
    """Belt defeated — a caller that never resolved a credential."""
    out = _ask(world, "acu-flowloop", context=False)
    assert out
    assert {c.site for c in out} <= {"acu-flowloop", SHARED_SITE}


def test_the_push_down_is_what_rescues_a_small_tenant(world) -> None:
    """The quality failure, not the safety one: with a post-fusion filter
    alone, the largest tenant fills the window and ACU gets almost nothing."""
    filtered_only = _ask(world, "acu-flowloop", corpora=False, breadth=8)
    with_push_down = _ask(world, "acu-flowloop", breadth=8)
    assert len(with_push_down) > len(filtered_only)


def test_every_tenant_sees_the_shared_corpus(world) -> None:
    """Visibility, not ranking: a tenant's own material legitimately outranks
    a glossary entry, so ask for enough room that this tests permission."""
    for site, _ in FACILITIES:
        out = _ask(world, site, limit=200, breadth=400)
        assert any(c.site == SHARED_SITE for c in out), site


def test_an_unattributed_chunk_reaches_nobody(world) -> None:
    """A chunk indexed before tenancy existed."""
    store, lookup, catalog = world
    store.chunks.append(
        {
            "site": None,
            "path": "/legacy/unlabelled.md",
            "title": "legacy",
            "chunk_index": 0,
            "corpus": "acu-flowloop:docs",  # even sitting in a tenant's corpus
            "chunk_text": "thermocouple temperature measurement data from before tenancy",
        }
    )
    for site, _ in FACILITIES:
        out = _ask(world, site, limit=50, breadth=200)
        assert not any(c.source_path == "/legacy/unlabelled.md" for c in out), site
