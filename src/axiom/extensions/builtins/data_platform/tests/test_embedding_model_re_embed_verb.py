# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi data embedding-model re-embed``: the verb, its declaration and its dry run (#2141).

What the re-embed does to a corpus is proven in
``axiom/rag/tests/test_re_embed_into_a_new_generation.py``. This file is about the surface: the
spelling reaches one skill, a dry run is the default, the skill is declared destructive so a
protocol surface plans before it applies, and the result says what to run next.
"""

from __future__ import annotations

import uuid

import pytest

import axiom.rag.embeddings as emb
from axiom.rag.chunker import Chunk

from ..commands.cli import _args_to_params, _parser, resolve_skill
from ..skills import _CHAINING, _SKILLS, _SPECS, bind, embedding_model, verbs

SKILL = "embedding_model_re_embed"
VEC = [0.01] * 768


def test_the_cli_spelling_reaches_the_re_embed_skill():
    args = _parser().parse_args(["embedding-model", "re-embed", "--corpus", "c1"])

    assert resolve_skill(args) == (SKILL, None)
    params = _args_to_params(args)
    assert params["corpus"] == "c1" and params["apply"] is False and params["batch"] == 2000
    assert "exclude_paths" not in params


def test_the_flags_reach_the_skill_as_params():
    args = _parser().parse_args(
        [
            "embedding-model",
            "re-embed",
            "--corpus",
            "c1",
            "--exclude-paths",
            "junk.txt",
            "--batch",
            "500",
            "--apply",
        ]
    )

    params = _args_to_params(args)

    assert (
        params["exclude_paths"] == "junk.txt" and params["batch"] == 500 and params["apply"] is True
    )


def test_verify_is_still_beside_it():
    args = _parser().parse_args(
        ["embedding-model", "verify", "--corpus", "c1", "--generation", "2"]
    )

    assert resolve_skill(args) == ("embedding_model_verify", None)


def test_the_skill_is_declared_destructive_for_protocol_surfaces():
    from axiom.infra.skills import SkillRegistry

    assert SKILL in _SKILLS and SKILL in verbs() and SKILL in _SPECS
    assert _CHAINING[SKILL]["risk"] == "destructive"
    registry = SkillRegistry()
    bind(registry)
    spec = registry.spec(f"data.{SKILL}")
    assert spec.effective_risk == "destructive"
    assert not ({"mcp", "agent_tool"} & set(spec.surfaces or ())), (
        "plan/apply is for a surface it has not opted into"
    )
    assert registry.spec("data.embedding_model_verify").effective_risk == "reversible-write"


def test_every_declared_param_is_one_the_parser_can_send():
    args = _parser().parse_args(
        [
            "embedding-model",
            "re-embed",
            "--corpus",
            "c",
            "--exclude-paths",
            "f",
            "--batch",
            "1",
            "--apply",
            "--dsn",
            "postgresql://x/y",
        ]
    )

    assert set(_args_to_params(args)) == set(_SPECS[SKILL][1])


# ---- against a database ---------------------------------------------------------------------------


@pytest.fixture
def dsn(real_postgres):
    return real_postgres.replace("postgresql+psycopg2://", "postgresql://", 1)


@pytest.fixture
def corpus(dsn, monkeypatch):
    from axiom.rag.store import RAGStore

    name = f"test-re-embed-verb-{uuid.uuid4().hex[:8]}"
    store = RAGStore(dsn)
    store.connect()
    for path, text in (
        ("ops/log.md", "Coolant inlet temperature averaged 38 degrees."),
        ("repo/venv/lib/site-packages/pkg/README.md", "a package readme"),
        ("drafts/old.md", "a draft nobody should retrieve"),
    ):
        store.upsert_chunks(
            [Chunk(text=text, source_path=path, source_title="T", chunk_index=0, start_line=1)],
            [VEC],
            corpus=name,
        )
    store.close()
    monkeypatch.setattr(
        emb,
        "_embed_guarded",
        lambda texts, model, tier: ([[0.02] * 768 for _ in texts], "test-embedder"),
    )
    return name


def _chunks(dsn, corpus, generation):
    from axiom.rag.store import RAGStore

    store = RAGStore(dsn)
    store.connect()
    try:
        return store.generation_chunk_count(corpus, generation)
    finally:
        store.close()


def test_the_skill_reports_without_apply_and_writes_nothing(dsn, corpus, tmp_path):
    patterns = tmp_path / "exclude.txt"
    patterns.write_text("# drafts\ndrafts/\n", encoding="utf-8")

    res = embedding_model.re_embed(
        {"corpus": corpus, "dsn": dsn, "exclude_paths": str(patterns)}, None
    )

    assert res.ok, res.errors
    assert res.value["apply"] is False and res.value["to_copy"] == {"documents": 1, "chunks": 1}
    assert res.value["excluded"]["by_default"] == 1 and res.value["excluded"]["by_file"] == 1
    assert "dry run" in res.actions_taken[0] and "test-embedder" in res.actions_taken[0]
    assert any(line.startswith("excluded: ") and "drafts/" in line for line in res.actions_taken)
    assert _chunks(dsn, corpus, 2) == 0


def test_the_skill_builds_the_candidate_with_apply_and_names_the_next_commands(dsn, corpus):
    res = embedding_model.re_embed({"corpus": corpus, "dsn": dsn, "apply": True, "batch": 10}, None)

    assert res.ok, res.errors
    assert res.value["copied"] == {"documents": 2, "chunks": 2} and res.value["promoted"] is False
    assert _chunks(dsn, corpus, 2) == 2
    nexts = [line for line in res.actions_taken if line.startswith("next: ")]
    assert f"next: axi data embedding-model verify --corpus {corpus} --generation 2" in nexts
    assert f"next: axi rag promote 2 --corpus {corpus}" in nexts
    assert any(f"axi rag rollback 1 --corpus {corpus}" in line for line in nexts)


def test_a_refusal_is_an_error_that_names_the_candidate(dsn, corpus):
    from axiom.rag.generation import GenerationManager
    from axiom.rag.store import RAGStore

    store = RAGStore(dsn)
    store.connect()
    manager = GenerationManager(store)
    manager.get_active_generation(corpus)
    manager.create_candidate(corpus)
    store.close()

    for params in ({"corpus": corpus, "dsn": dsn}, {"corpus": corpus, "dsn": dsn, "apply": True}):
        res = embedding_model.re_embed(params, None)
        assert res.ok is False and "candidate generation 2" in res.errors[0]


def test_a_sqlite_store_is_refused_with_a_reason(tmp_path):
    res = embedding_model.re_embed(
        {"corpus": "rag-internal", "dsn": f"sqlite:///{tmp_path}/rag.db"}, None
    )

    assert res.ok is False and "one generation" in res.errors[0]


def test_a_missing_corpus_or_exclusion_file_is_an_error(dsn, tmp_path):
    assert embedding_model.re_embed({"dsn": dsn}, None).ok is False
    res = embedding_model.re_embed(
        {"corpus": "c", "dsn": dsn, "exclude_paths": str(tmp_path / "absent.txt")}, None
    )
    assert res.ok is False and "exclusion file" in res.errors[0]
