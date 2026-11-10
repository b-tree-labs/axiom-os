# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Embedding failure instrumentation: self-describing + classified + bounded."""
from __future__ import annotations

import pytest

from axiom.rag.embeddings import (
    EmbeddingError,
    PersistentEmbeddingError,
    TransientEmbeddingError,
    sanitize_for_embedding,
)


def test_sanitize_strips_control_and_caps():
    assert sanitize_for_embedding("ab\x00\x0c\x07cd\x7f ef") == "abcd ef"
    assert len(sanitize_for_embedding("x" * 50000)) == 8000
    assert sanitize_for_embedding("") == ""


def test_error_hierarchy_and_real_detail():
    p = PersistentEmbeddingError("rejected", status=400, body="input too long",
                                 provider="ollama")
    assert isinstance(p, EmbeddingError)
    assert not isinstance(p, TransientEmbeddingError)   # 4xx must NOT look retryable
    assert p.status == 400
    s = str(p)
    assert "ollama" in s and "400" in s and "input too long" in s  # real detail surfaced


def test_transient_is_distinct():
    t = TransientEmbeddingError("all failed", provider="all")
    assert isinstance(t, EmbeddingError)
    assert not isinstance(t, PersistentEmbeddingError)


def test_classification_drives_retry_decision():
    # The caller distinguishes by type: persistent → quarantine, transient → retry.
    def handle(exc):
        return "quarantine" if isinstance(exc, PersistentEmbeddingError) else "retry"
    assert handle(PersistentEmbeddingError("x", status=400)) == "quarantine"
    assert handle(TransientEmbeddingError("x")) == "retry"
    with pytest.raises(EmbeddingError):
        raise PersistentEmbeddingError("x", status=422)
