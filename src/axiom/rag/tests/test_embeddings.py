# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""embed_texts must distinguish two None-looking outcomes.

A transient network drop to the embedder and "no provider configured at all"
both used to surface as `None`, so the ingest layer could not tell them apart
and would silently commit a doc text-only after a network failure. embed_texts
now returns None ONLY when no provider is configured, and raises EmbeddingError
when a provider IS configured but every attempt failed.
"""

from __future__ import annotations

import pytest

from axiom.rag import embeddings as emb
from axiom.rag.embeddings import EmbeddingError, embed_texts


def _all_providers_return_none(monkeypatch):
    monkeypatch.setattr(emb, "_embed_remote", lambda texts: None)
    monkeypatch.setattr(emb, "_embed_openai", lambda texts, model: None)
    monkeypatch.setattr(emb, "_embed_ollama", lambda texts: None)


def test_empty_texts_returns_empty():
    assert embed_texts([]) == []


def test_no_provider_configured_returns_none(monkeypatch):
    monkeypatch.delenv("NEUT_EMBED_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(emb, "_ollama_reachable", lambda: False)
    _all_providers_return_none(monkeypatch)
    assert embed_texts(["hello"]) is None


def test_configured_remote_but_failing_raises(monkeypatch):
    monkeypatch.setenv("NEUT_EMBED_URL", "https://embedder.example:42000")
    monkeypatch.setattr(emb, "_ollama_reachable", lambda: False)
    _all_providers_return_none(monkeypatch)
    with pytest.raises(EmbeddingError):
        embed_texts(["hello"])


def test_configured_ollama_but_failing_raises(monkeypatch):
    monkeypatch.delenv("NEUT_EMBED_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(emb, "_ollama_reachable", lambda: True)
    _all_providers_return_none(monkeypatch)
    with pytest.raises(EmbeddingError):
        embed_texts(["hello"])
