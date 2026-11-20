# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Embedding-space registry (ADR-087 D6).

Spaces keyed ``(model, dim)``. The canonical space is always maintained;
secondary spaces embed lazily (only on demand). Matryoshka truncation projects
a native-dim vector down to a smaller space where the embedder supports it. A
query-time embed endpoint means consumers never have to match spaces. Dedup
(P2) matches in the pinned canonical space (resolves OQ2).
"""

from __future__ import annotations

import hashlib

import pytest

from axiom.memory.embedding_registry import (
    EmbeddingSpace,
    EmbeddingSpaceRegistry,
)


def _embedder(dim):
    """Deterministic native-dim embedder that counts its calls."""
    calls = {"n": 0}

    def embed(texts):
        calls["n"] += 1
        out = []
        for t in texts:
            h = hashlib.sha256(t.lower().encode()).digest()
            out.append([(h[i % len(h)]) / 255.0 for i in range(dim)])
        return out

    embed.calls = calls
    return embed


class TestSpaceKeying:
    def test_space_key_is_model_dim(self):
        space = EmbeddingSpace(model="nomic-embed-text", dim=768)
        assert space.key == ("nomic-embed-text", 768)

    def test_resolve_by_model_and_dim(self):
        canonical = EmbeddingSpace("nomic-embed-text", 768)
        reg = EmbeddingSpaceRegistry(canonical=canonical, embedder=_embedder(768))
        assert reg.resolve("nomic-embed-text", 768) is canonical

    def test_resolve_unknown_space_raises(self):
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 8), embedder=_embedder(8)
        )
        with pytest.raises(KeyError):
            reg.resolve("other", 16)


class TestCanonicalAlwaysMaintained:
    def test_embed_defaults_to_canonical(self):
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 8), embedder=_embedder(8)
        )
        vecs = reg.embed(["hello"])
        assert len(vecs) == 1 and len(vecs[0]) == 8

    def test_pinned_embedder_is_canonical_space(self):
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 8), embedder=_embedder(8)
        )
        pinned = reg.pinned_embedder()
        assert len(pinned(["x"])[0]) == 8


class TestSecondaryLazyAndMatryoshka:
    def test_secondary_space_embeds_lazily(self):
        native = _embedder(16)
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 16), embedder=native
        )
        secondary_embedder = _embedder(16)
        reg.register_secondary(
            EmbeddingSpace("m", 8, matryoshka=True), embedder=secondary_embedder
        )
        # Registration alone must not embed anything (lazy).
        assert secondary_embedder.calls["n"] == 0
        reg.embed(["hi"], model="m", dim=8)
        assert secondary_embedder.calls["n"] == 1

    def test_matryoshka_truncates_and_renormalizes(self):
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 16), embedder=_embedder(16)
        )
        reg.register_secondary(
            EmbeddingSpace("m", 8, matryoshka=True), embedder=_embedder(16)
        )
        vec = reg.embed(["hello world"], model="m", dim=8)[0]
        assert len(vec) == 8
        norm = sum(x * x for x in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-9  # renormalized to unit length

    def test_non_matryoshka_dim_mismatch_raises(self):
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 16), embedder=_embedder(16)
        )
        reg.register_secondary(
            EmbeddingSpace("m", 8, matryoshka=False), embedder=_embedder(16)
        )
        with pytest.raises(ValueError, match="matryoshka"):
            reg.embed(["x"], model="m", dim=8)


class TestQueryTimeEndpoint:
    def test_embed_query_resolves_space_for_consumer(self):
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 8), embedder=_embedder(8)
        )
        # Consumer names neither model nor dim — gets the canonical space.
        vecs = reg.embed_query(["a query"])
        assert len(vecs[0]) == 8


class TestDedupInPinnedSpace:
    def test_dedup_matches_in_pinned_canonical_space(self):
        """OQ2: dedup uses the registry's pinned canonical embedder, so tier
        decisions are not silently model-dependent."""
        from axiom.memory.dedup import DedupEngine, MatchTier

        # A canonical embedder that collapses on near-identical text.
        def canonical_embed(texts):
            out = []
            for t in texts:
                base = "test driven development" in t.lower()
                out.append([1.0, 0.0] if base else [0.0, 1.0])
            return out

        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("canon", 2), embedder=canonical_embed
        )
        engine = DedupEngine(embedder=reg.pinned_embedder())
        tier = engine.classify_pair(
            "prefers test driven development",
            "she prefers test driven development strongly",
        )
        assert tier is MatchTier.NEAR_DUP

    def test_adding_secondary_space_does_not_shift_pinned_embedder(self):
        reg = EmbeddingSpaceRegistry(
            canonical=EmbeddingSpace("m", 8), embedder=_embedder(8)
        )
        pinned = reg.pinned_embedder()
        before = pinned(["stable text"])[0]
        reg.register_secondary(
            EmbeddingSpace("m", 4, matryoshka=True), embedder=_embedder(8)
        )
        after = pinned(["stable text"])[0]
        assert before == after  # canonical pin is stable under registry growth
