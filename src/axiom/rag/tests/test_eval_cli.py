# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi rag eval`` — CLI verb for the eval harness.

Wraps :mod:`axiom.rag.eval` with an operator surface: load YAML
question set, build a model_fn from the gateway + an optional
retriever_fn from the RAG store, run the comparison, print a report.
"""

from __future__ import annotations

from unittest.mock import patch


class _FakeStore:
    """Just enough of RAGStore.search to drive eval CLI tests."""

    def __init__(self, hits):
        self._hits = hits

    def search(self, **kwargs):
        return self._hits

    def close(self):
        pass


def _make_yaml(tmp_path):
    f = tmp_path / "q.yaml"
    f.write_text(
        "- id: q1\n"
        "  question: 'What moderator did CP2 use?'\n"
        "  expected_answer_contains: ['graphite']\n"
    )
    return f


# -- baseline mode (no retrieval) --------------------------------------------


def test_cli_eval_no_retrieval_calls_model_only(tmp_path, capsys):
    """--no-retrieval skips the RAG path entirely; model gets bare prompts."""
    from axiom.rag import cli as rag_cli

    yaml_path = _make_yaml(tmp_path)
    calls = {"model": [], "search": 0}

    def fake_model(prompt, *, context=None):
        calls["model"].append((prompt, context))
        return "Graphite was the moderator."

    with patch("axiom.rag.cli._get_store", return_value=_FakeStore([])), \
         patch("axiom.rag.cli._build_model_fn", return_value=fake_model):
        rag_cli.main(["eval", "--questions", str(yaml_path), "--no-retrieval"])

    out = capsys.readouterr().out
    assert "mean answer score" in out.lower() or "score" in out.lower()
    assert len(calls["model"]) == 1
    assert calls["model"][0][1] is None   # no context passed


# -- with retrieval ----------------------------------------------------------


def test_cli_eval_with_retrieval_uses_the_retriever(tmp_path, capsys):
    """Answer mode retrieves through retrieve() (store-shaped hits), as chat does."""
    from axiom.rag import cli as rag_cli
    from axiom.rag.store import SearchResult

    yaml_path = _make_yaml(tmp_path)

    hit = SearchResult(
        source_path="cp2.pdf", source_title="cp2.pdf",
        chunk_text="CP2 used graphite as the moderator.", chunk_index=0,
        similarity=0.9, combined_score=0.9,
    )
    store = _FakeStore([hit])

    def fake_model(prompt, *, context=None):
        return "Graphite." if context and "graphite" in context else "Unknown."

    with patch("axiom.rag.cli._get_store", return_value=store), \
         patch("axiom.rag.cli._build_model_fn", return_value=fake_model), \
         patch("axiom.rag.embeddings.embed_texts", return_value=None):
        rag_cli.main(["eval", "--questions", str(yaml_path)])

    out = capsys.readouterr().out
    # Comparison mode default — baseline + with_rag + lift
    assert "lift (with - baseline) on mean answer score: +1.000" in out


# -- error paths -------------------------------------------------------------


def test_cli_eval_missing_questions_file_exits_nonzero(tmp_path, capsys):
    import pytest

    from axiom.rag import cli as rag_cli

    with patch("axiom.rag.cli._get_store", return_value=_FakeStore([])):
        with pytest.raises(SystemExit) as ei:
            rag_cli.main(["eval", "--questions", "/no/such/file.yaml"])
        assert ei.value.code != 0
