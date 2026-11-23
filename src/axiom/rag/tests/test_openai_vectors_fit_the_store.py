# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""OpenAI's text-embedding-3 models are asked for vectors the RAG store's column can hold."""

from __future__ import annotations

from axiom.rag import embeddings as E


def test_openai_is_asked_for_vectors_the_store_can_hold(monkeypatch):
    """``text-embedding-3-*`` returns 1,536 numbers unless asked; the store's column holds 768."""
    import requests

    sent: dict = {}

    class _Resp:
        status_code, text, headers = 200, "", {}

        def json(self):
            return {"data": [{"index": 0, "embedding": [0.0] * 768}]}

    def post(url, headers=None, json=None, timeout=None, **kwargs):
        sent.update(json)
        return _Resp()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(E, "_openai_quota_blocked", False)
    E._embed_openai(["t"], "text-embedding-3-small")
    assert sent["dimensions"] == 768
