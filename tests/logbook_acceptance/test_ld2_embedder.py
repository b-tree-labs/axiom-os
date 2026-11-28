# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LD-2 follow-up (#2185): the log index's default embedder.

The node's local embedding model when it has one; the built-in hashing embedder when not; a
remote provider only on the site's explicit opt-in. The resolver is stubbed: no model is called.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.attest import index
from axiom.rag import embeddings


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for name in (index.MODEL_ENV, index.DIM_ENV, index.PIN_ENV, index.ALLOW_REMOTE_ENV):
        monkeypatch.delenv(name, raising=False)
    index.set_registry(None)
    yield
    index.set_registry(None)


def _stub(monkeypatch, *, local, remote_configured=False):
    monkeypatch.setattr(embeddings, "local_embedding_model", lambda: local)
    monkeypatch.setattr(embeddings, "provider_configured", lambda tier="any": remote_configured)
    calls = {"local": 0, "chain": 0}

    def fake_local(texts):
        calls["local"] += 1
        return [[0.5] * 768 for _ in texts]

    def fake_chain(texts, model="x", *, routing_tier="any"):
        calls["chain"] += 1
        return [[0.5] * 768 for _ in texts], "nomic-embed-text"

    monkeypatch.setattr(embeddings, "embed_texts_local", fake_local)
    monkeypatch.setattr(embeddings, "embed_texts_with_model", fake_chain)
    return calls


def test_a_node_with_a_local_model_indexes_with_it(monkeypatch):
    calls = _stub(monkeypatch, local="nomic-embed-text")
    reg = index.registry()
    assert (reg.canonical.model, reg.canonical.dim) == ("nomic-embed-text", 768)
    assert reg.embed(["a record"]) is not None
    assert calls["local"] == 1
    assert calls["chain"] == 0, "the local default never goes through the remote-capable chain"


def test_a_node_with_no_local_model_falls_back_to_hashing(monkeypatch):
    _stub(monkeypatch, local=None)
    assert index.registry().canonical == index.LEXHASH


def test_a_remote_only_configuration_falls_back_to_hashing(monkeypatch):
    calls = _stub(monkeypatch, local=None, remote_configured=True)
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", "https://embed.example.invalid")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    reg = index.registry()
    assert reg.canonical == index.LEXHASH
    assert calls == {"local": 0, "chain": 0}


def test_the_site_opt_in_allows_the_remote_chain(monkeypatch):
    _stub(monkeypatch, local=None, remote_configured=True)
    monkeypatch.setenv(index.ALLOW_REMOTE_ENV, "1")
    assert index.registry().canonical.model != index.LEXHASH.model


def test_the_opt_in_with_nothing_configured_still_hashes(monkeypatch):
    _stub(monkeypatch, local=None, remote_configured=False)
    monkeypatch.setenv(index.ALLOW_REMOTE_ENV, "1")
    assert index.registry().canonical == index.LEXHASH


def test_the_pin_holds_even_when_a_local_model_exists(monkeypatch):
    _stub(monkeypatch, local="nomic-embed-text")
    monkeypatch.setenv(index.PIN_ENV, "hashing")
    assert index.registry().canonical == index.LEXHASH


def test_the_resolution_is_kept_between_calls(monkeypatch):
    _stub(monkeypatch, local="nomic-embed-text")
    probes = []
    monkeypatch.setattr(
        embeddings, "local_embedding_model", lambda: probes.append(1) or "nomic-embed-text"
    )
    index.registry()
    index.registry()
    assert len(probes) == 1
