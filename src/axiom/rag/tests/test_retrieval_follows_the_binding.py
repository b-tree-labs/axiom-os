# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A laptop bound to a site node grounds chat on that node's documents.

Found when a colleague's bound laptop printed ``RAG: none``: data reads already
followed the binding, document retrieval did not, and the retrieval client could
only speak the federation headers a person does not hold. Retrieval now follows
the binding when nothing else is configured, and sends the binding's key, the
same key a bound data read sends, to that node and no other.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.data_platform import read_target
from axiom.rag import remote_store as rs
from axiom.rag.remote_store import RemoteRetrievalError, RemoteRetrievalStore
from axiom.rag.store_factory import create_store, resolve_store_url

FACE = "https://node.example.org"
KEY = "axk_colleague-secret-value"


class _Settings:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture
def settings(monkeypatch):
    values: dict = {}
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore", lambda: _Settings(values)
    )
    return values


@pytest.fixture
def bound(tmp_path, monkeypatch, settings):
    """A laptop bound to ``alpha-site`` at :data:`FACE`, whose key resolves to :data:`KEY`."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    for name in ("DATABASE_URL", "AXIOM_RAG_REMOTE_SIGNATURE", "AXIOM_RAG_REMOTE_NODE_ID"):
        monkeypatch.delenv(name, raising=False)
    (state / read_target.STATE_FILENAME).write_text(
        json.dumps(
            {
                "current": "alpha-site",
                "bindings": {
                    "alpha-site": {
                        "site": "alpha-site",
                        "face_url": FACE,
                        "token_ref": "openbao://kv/data/alpha-key",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    asked: list[str] = []

    def _key(face_url: str) -> str:
        asked.append(face_url)
        return KEY

    monkeypatch.setattr(read_target, "_key", _key)
    return asked


def _spy(monkeypatch, status=200, body=None):
    captured: dict = {}

    def _post(url, payload, headers, timeout):
        captured.update(url=url, headers=dict(headers), payload=payload)
        return status, body if body is not None else {"results": []}

    monkeypatch.setattr(rs, "_post_json", _post)
    return captured


def test_with_nothing_configured_retrieval_follows_the_binding(bound):
    resolved = resolve_store_url()
    assert resolved.url == FACE
    assert resolved.site == "alpha-site"
    assert "site binding" in resolved.origin


def test_a_configured_store_wins_over_the_binding(bound, settings, monkeypatch):
    settings["rag.database_url"] = "postgresql://u@db.example/rag"
    assert resolve_store_url().url == "postgresql://u@db.example/rag"
    settings.clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@local/rag")
    assert resolve_store_url().url == "postgresql://u@local/rag"
    # A surface that does not read DATABASE_URL still treats it as configured: never the binding.
    assert resolve_store_url(honour_env=False).url == ""


def test_the_bound_store_sends_the_binding_key_as_a_bearer(bound, monkeypatch):
    captured = _spy(monkeypatch)
    store = create_store(resolve_store_url().url)
    store.search(query_text="how is the archive kept", limit=3)
    assert captured["url"] == f"{FACE}/api/v1/rag/search"
    assert captured["headers"]["Authorization"] == f"Bearer {KEY}"
    # Resolved by read_target's own key resolution, for the bound face.
    assert bound == [FACE]
    # A person is not a peer: no federation headers unless a peer identity was configured.
    assert "X-Node-ID" not in captured["headers"]
    assert "X-Signature" not in captured["headers"]


def test_a_peer_with_the_federation_secret_still_signs(bound, monkeypatch):
    monkeypatch.setenv("AXIOM_RAG_REMOTE_SIGNATURE", "peer-secret")
    monkeypatch.setenv("AXIOM_RAG_REMOTE_NODE_ID", "peer-1")
    captured = _spy(monkeypatch)
    create_store("https://peer.example.net").search(query_text="q")
    assert captured["headers"]["X-Node-ID"] == "peer-1"
    assert captured["headers"]["X-Signature"] == "peer-secret"
    assert "Authorization" not in captured["headers"]  # the binding's key goes to its node only


def test_the_key_is_never_offered_to_another_host(bound, monkeypatch):
    captured = _spy(monkeypatch)
    create_store("https://elsewhere.example.net").search(query_text="q")
    assert "Authorization" not in captured["headers"]
    assert bound == []  # the key was not even resolved


def test_a_refusal_names_the_problem_and_never_the_key(bound, monkeypatch, caplog):
    _spy(monkeypatch, status=403, body={"error": {"message": f"bad token {KEY}"}})
    store = create_store(FACE)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RemoteRetrievalError) as err:
        store.search(query_text="q")
    assert KEY not in str(err.value)
    assert "refused this binding's key" in str(err.value)
    assert KEY not in caplog.text


def test_a_bearer_store_built_directly_keeps_peer_headers_only_when_asked():
    plain = RemoteRetrievalStore(FACE, bearer=KEY)
    assert plain._federated is False
    signed = RemoteRetrievalStore(FACE, bearer=KEY, signature="s")
    assert signed._federated is True
    assert RemoteRetrievalStore(FACE)._federated is True  # unchanged default for peers


def test_every_surface_that_builds_the_chat_store_follows_the_binding(bound):
    from axiom.extensions.builtins.chat import planes, where
    from axiom.extensions.builtins.mcp import platform_primitives

    domain = planes.resolve_domain()
    assert domain.kind == "served" and domain.source == FACE
    assert "site binding alpha-site" in domain.detail
    r = where._retrieval_where()
    assert r.kind == "remote-node" and r.host == "node.example.org" and r.site == "alpha-site"

    store = platform_primitives._resolve_rag_store()
    assert isinstance(store, RemoteRetrievalStore) and store._bearer == KEY

    from axiom.extensions.builtins.chat.agent import ChatAgent

    agent = ChatAgent.__new__(ChatAgent)
    agent._rag_init_attempted = False
    agent._rag_store = None
    chat_store = agent._get_rag_store()
    assert isinstance(chat_store, RemoteRetrievalStore) and chat_store._bearer == KEY
