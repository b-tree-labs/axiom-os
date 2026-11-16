# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VAR-A3: a Library upload in one variant is not retrievable by another's chat or search.

The variants share one data platform and one retrieval store. What keeps them
apart is where an upload lands (``library.scope.corpus_for``) and what each
variant's chat (``HeadlessChat.new_scope``) and search (``search.engine``) may
read. These tests write a real chunk into a real SQLite RAG store through the
library's routing in one variant, then retrieve through each other variant's
corpora.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.extensions.builtins.library import scope as lib
from axiom.extensions.builtins.search import engine
from axiom.infra import variants as v
from axiom.infra.bus import EventBus
from axiom.rag.chunker import Chunk
from axiom.rag.sqlite_store import SQLiteRAGStore

MARKER = "zanzibar-quartz uploaded here"


@pytest.fixture(autouse=True)
def _plain(tmp_path, monkeypatch):
    monkeypatch.setenv(v.CONFIG_ENV, str(tmp_path / "absent.toml"))
    monkeypatch.delenv(v.VARIANT_ENV, raising=False)
    monkeypatch.setenv("AXIOM_SITE", "example-home")
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)


@pytest.fixture
def store(tmp_path):
    st = SQLiteRAGStore(f"sqlite:///{tmp_path / 'rag.db'}")
    st.connect()
    yield st
    st.close()


def _in(monkeypatch, name: str) -> None:
    monkeypatch.setenv(v.VARIANT_ENV, name)


def _upload(monkeypatch, store, name: str, visibility: str = "site") -> str:
    """A Library upload by the same person in variant ``name``; returns its corpus."""
    _in(monkeypatch, name)
    caller = lib.make_caller("@ada:example-home", "example-home", surface="web")
    corpus = lib.corpus_for(caller, visibility)
    store.upsert_chunks(
        [Chunk(source_path=f"{name}/{visibility}.md", source_title="Upload", source_type="md",
               text=MARKER, chunk_index=0, start_line=1)],
        corpus=corpus,
    )
    return corpus


def _chat_corpora(tmp_path) -> list[str]:
    headless = HeadlessChat(turn_deadline=30.0, gateway=MagicMock(),
                            bus=EventBus(log_path=tmp_path / "events.jsonl"))
    # The request names a tenant; an isolated variant overrides it.
    return list(headless.new_scope(tenant_id=None).retrieval_corpora)


def _found(store, corpora) -> bool:
    return any(MARKER in r.chunk_text for r in store.search(query_text="zanzibar", corpora=corpora))


PAIRS = [(a, b) for a in v.NAMES for b in v.NAMES if a != b]


@pytest.mark.parametrize("home,other", PAIRS)
def test_var_a3_an_upload_in_one_variant_is_not_in_anothers_chat(monkeypatch, store, tmp_path,
                                                                  home, other):
    _upload(monkeypatch, store, home)
    _in(monkeypatch, other)
    assert not _found(store, _chat_corpora(tmp_path))


@pytest.mark.parametrize("home,other", PAIRS)
def test_var_a3_an_upload_in_one_variant_is_not_in_anothers_search(monkeypatch, store, home, other):
    _upload(monkeypatch, store, home)
    _in(monkeypatch, other)
    assert not _found(store, engine.variant_corpora())


@pytest.mark.parametrize("home", v.NAMES)
def test_var_a3_an_upload_is_in_its_own_variants_chat_and_search(monkeypatch, store, tmp_path, home):
    """The control: the same retrieval does find it at home, so the negatives mean something."""
    _upload(monkeypatch, store, home)
    _in(monkeypatch, home)
    assert _found(store, _chat_corpora(tmp_path))
    assert _found(store, engine.variant_corpora())


@pytest.mark.parametrize("home,other", PAIRS)
def test_var_a3_a_private_upload_is_not_in_anothers_library(monkeypatch, store, home, other):
    """The same person signed in to two variants holds two libraries."""
    _upload(monkeypatch, store, home, visibility="private")
    _in(monkeypatch, other)
    caller = lib.make_caller("@ada:example-home", "example-home")
    assert not _found(store, lib.readable_corpora(caller))


def test_var_a3_an_isolated_variant_ignores_the_site_a_credential_names(monkeypatch):
    _in(monkeypatch, "product")
    caller = lib.make_caller("@ada:example-home", "example-home")
    assert caller.tenant and caller.site == "variant-product"


def test_var_a3_a_plain_install_is_unchanged(monkeypatch):
    caller = lib.make_caller("@ada:example-home", "example-home")
    assert lib.corpus_for(caller, "site") == "rag-org"
    assert engine.variant_corpora() is None
