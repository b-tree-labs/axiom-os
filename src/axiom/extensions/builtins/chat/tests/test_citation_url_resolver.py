# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A citation carries a link when the deployment can serve the document.

Measured on a node 2026-09-25: chat answers named sources and linked none, and
the only structured citation form carried key/rank/title/path/corpus and no url.
A name without a link is not provenance the reader can check. The platform does
not know how a node serves documents, so the resolver is supplied by the caller;
what the platform guarantees is that the field is always present, is None when
nothing can serve the document, and that a broken resolver never drops the
citation itself.
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.extensions.builtins.chat.providers.base import citation_entries


def _chunk(path, rank=1, title="", key="C1"):
    return SimpleNamespace(
        source_path=path, rank=rank, source_title=title, citation_key=key, corpus="rag-org"
    )


def test_url_is_present_and_none_without_a_resolver():
    (e,) = citation_entries([_chunk("/ops/rod.md")])
    assert "url" in e and e["url"] is None


def test_a_resolver_fills_the_url_from_the_source_path():
    (e,) = citation_entries(
        [_chunk("/ops/rod.md")], url_for=lambda p: f"https://node/documents?path={p}"
    )
    assert e["url"] == "https://node/documents?path=/ops/rod.md"


def test_a_resolver_may_decline_a_document_it_cannot_serve():
    e1, e2 = citation_entries(
        [_chunk("/ops/rod.md", rank=1, key="C1"), _chunk("/ops/old.txt", rank=2, key="C2")],
        url_for=lambda p: "https://node/d/1" if p.endswith("rod.md") else None,
    )
    assert e1["url"] == "https://node/d/1"
    assert e2["url"] is None, "declined is None, not a dead link"


def test_a_raising_resolver_never_drops_the_citation():
    def boom(_p):
        raise RuntimeError("signer down")

    (e,) = citation_entries([_chunk("/ops/rod.md")], url_for=boom)
    assert e["title"] and e["url"] is None


def test_one_url_per_source_even_with_many_chunks():
    calls = []

    def res(p):
        calls.append(p)
        return f"u:{p}"

    entries = citation_entries(
        [_chunk("/a.md", 3), _chunk("/a.md", 1), _chunk("/b.md", 2)], url_for=res
    )
    assert [e["path"] for e in entries] == ["/a.md", "/b.md"]
    assert calls.count("/a.md") == 1, "resolved once per source, not per chunk"
