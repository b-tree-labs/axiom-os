# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``retrieve()`` filters unless a caller explicitly says not to.

The default used to be the other way: ``access_context=None`` meant "apply no
filter", so a caller who simply did not pass one got every chunk regardless of
tier or classification. That is the wrong direction for a default on a
retrieval path, and it is the insidious kind of wrong, because the call that
leaks looks exactly like the call that does not.

Two changes are pinned here. Omitting the argument now applies the fail-closed
baseline, and passing ``None`` does too, so a variable that is unexpectedly
``None`` narrows rather than widens. Retrieving genuinely unfiltered is still
possible, because a remote store that filters server-side needs it, but it now
requires the named :data:`UNFILTERED` sentinel, which is greppable and shows up
in review.
"""

from __future__ import annotations

from axiom.rag.retriever import UNFILTERED, AccessContext, retrieve


class _Hit:
    """A chunk the fail-closed baseline must refuse."""

    source_path = "classified.md"
    source_title = "t"
    chunk_text = "body"
    chunk_index = 0
    corpus = "rag-org"
    similarity = 0.9
    access_tier = "classified"
    classification = "secret"


class _PublicHit:
    source_path = "ordinary.md"
    source_title = "t"
    chunk_text = "body"
    chunk_index = 0
    corpus = "rag-org"
    similarity = 0.9
    access_tier = "public"
    classification = "unclassified"


class _Store:
    def __init__(self, *hits):
        self._hits = hits or (_Hit(),)

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **kw):
        return list(self._hits)


def _paths(chunks):
    return [c.source_path for c in chunks]


class TestOmittingTheContextFiltersRatherThanLeaks:
    def test_omitted_refuses_a_classified_chunk(self):
        got = retrieve(_Store(), query_text="q", query_embedding=None)
        assert _paths(got) == []

    def test_omitted_still_admits_ordinary_content(self):
        """Fail-closed must not mean 'returns nothing'."""
        got = retrieve(_Store(_PublicHit()), query_text="q", query_embedding=None)
        assert _paths(got) == ["ordinary.md"]

    def test_omitted_admits_the_public_one_and_refuses_the_classified_one(self):
        got = retrieve(_Store(_Hit(), _PublicHit()), query_text="q", query_embedding=None)
        assert _paths(got) == ["ordinary.md"]


class TestAnAccidentalNoneNarrowsRatherThanWidens:
    """The variable-is-unexpectedly-None bug must be safe, not catastrophic."""

    def test_none_filters_exactly_as_omission_does(self):
        got = retrieve(_Store(), query_text="q", query_embedding=None, access_context=None)
        assert _paths(got) == []

    def test_none_and_omitted_agree(self):
        a = retrieve(_Store(_Hit(), _PublicHit()), query_text="q", query_embedding=None)
        b = retrieve(
            _Store(_Hit(), _PublicHit()),
            query_text="q",
            query_embedding=None,
            access_context=None,
        )
        assert _paths(a) == _paths(b)


class TestUnfilteredIsStillPossibleButMustBeSaidOutLoud:
    """A remote store that filters server-side needs this; it must be explicit."""

    def test_unfiltered_returns_the_classified_chunk(self):
        got = retrieve(_Store(), query_text="q", query_embedding=None, access_context=UNFILTERED)
        assert _paths(got) == ["classified.md"]

    def test_unfiltered_is_not_none_so_it_cannot_arise_by_accident(self):
        assert UNFILTERED is not None
        assert not isinstance(UNFILTERED, AccessContext)


class TestAnExplicitContextIsUnchanged:
    def test_an_explicit_baseline_refuses_the_classified_chunk(self):
        got = retrieve(
            _Store(),
            query_text="q",
            query_embedding=None,
            access_context=AccessContext(),
        )
        assert _paths(got) == []

    def test_an_elevated_context_admits_it(self):
        got = retrieve(
            _Store(),
            query_text="q",
            query_embedding=None,
            access_context=AccessContext(
                max_access_tier="classified",
                allowed_classifications=frozenset({"secret"}),
            ),
        )
        assert _paths(got) == ["classified.md"]


class TestAnUnrecognisedTierDeniesRatherThanRanksLow:
    """The tier vocabularies in this codebase are not identical.

    ``_TIER_ORDER`` knows public/course/institutional/classified. The store's
    own ordering and the MCP path emit ``export_controlled`` and
    ``restricted``. That divergence is safe only because an unknown tier
    resolves to 99 on the chunk (deny) and to 0 on the context (public), so it
    fails closed from both sides. These pin that, because the day it silently
    stops being true is the day export-controlled material is retrievable.
    """

    class _EcHit(_Hit):
        source_path = "ec.md"
        access_tier = "export_controlled"
        classification = "unclassified"

    def test_an_unknown_chunk_tier_is_denied_at_the_baseline(self):
        got = retrieve(_Store(self._EcHit()), query_text="q", query_embedding=None)
        assert _paths(got) == []

    def test_an_unknown_chunk_tier_is_denied_even_when_the_context_names_it(self):
        got = retrieve(
            _Store(self._EcHit()),
            query_text="q",
            query_embedding=None,
            access_context=AccessContext(max_access_tier="export_controlled"),
        )
        assert _paths(got) == []

    def test_only_the_explicit_sentinel_reaches_it(self):
        got = retrieve(
            _Store(self._EcHit()),
            query_text="q",
            query_embedding=None,
            access_context=UNFILTERED,
        )
        assert _paths(got) == ["ec.md"]
