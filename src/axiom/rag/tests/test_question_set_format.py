# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A retrieval question set: one format, a public sample in CI, a real set kept on its node.

The defaults for the rerank and the passage count are to be settled from real questions. Those
questions come from a site's own chat traces, over its own documents, and can be sensitive, so
the real set is stored on the site's node and never in a repository. What lives here is the
format, the loader that reads a set from any path on the node, and a public sample over
invented documents that runs in CI with the same command a site runs.

Acceptance rows:

- QSET-1: a question names its expected document, expected pages and intent; older files load.
- QSET-2: ``--questions <path>`` reads a set from outside the repository, YAML or JSON Lines,
  and a malformed set is refused with the question and field named.
- QSET-3: a retrieval-only run reports per intent as well as overall.
- QSET-4: the public sample has 5 to 10 questions, each with an intent, over documents the
  sample holds.
- QSET-5: the public sample runs with
  ``axi rag eval --questions <sample> --retrieval-only --rerank both --k 8``.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from axiom.rag.tests.test_one_retrieval_path import _embed, sample_store  # noqa: F401 - fixture


def _write(tmp_path, name: str, body: str):
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f


# -- QSET-1 ------------------------------------------------------------------


def test_qset_1_expected_document_pages_and_intent_load(tmp_path):
    from axiom.rag.eval import load_questions

    qs = load_questions(_write(tmp_path, "q.yaml", (
        "- id: a\n"
        "  question: How long is a loan?\n"
        "  expected_document: Lending Policy.pdf\n"
        "  expected_pages: [3, 4]\n"
        "  intent: lookup\n"
        "- id: b\n"
        "  question: Which rooms are there?\n"
        "  expected_document: [Room Booking Guide, rooms/booking.md]\n"
        "  intent: synthesis\n"
    )))
    assert qs[0].expected_sources == ["Lending Policy.pdf"]
    assert qs[0].expected_pages == [3, 4]
    assert qs[0].intent == "lookup"
    assert qs[1].expected_sources == ["Room Booking Guide", "rooms/booking.md"]
    assert qs[1].intent == "synthesis"


def test_qset_1_a_file_without_the_new_fields_still_loads(tmp_path):
    from axiom.rag.eval import load_questions

    q = load_questions(_write(tmp_path, "q.yaml", "- id: a\n  question: x\n  expected_sources: [a.md]\n"))[0]
    assert q.expected_sources == ["a.md"] and q.intent == ""


# -- QSET-2 ------------------------------------------------------------------


def test_qset_2_a_set_on_the_node_in_json_lines_loads(tmp_path):
    """A set exported from traces is often one JSON object per line."""
    from axiom.rag.eval import load_questions

    rows = [{"id": "t1", "question": "When is the branch open?", "expected_document": "Opening Hours.md",
             "expected_pages": [1], "intent": "operations"},
            {"id": "t2", "question": "Late fees?", "expected_document": "Lending Policy.pdf", "intent": "lookup"}]
    path = _write(tmp_path, "set.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n\n")
    qs = load_questions(path)
    assert [q.id for q in qs] == ["t1", "t2"]
    assert qs[0].expected_sources == ["Opening Hours.md"] and qs[0].intent == "operations"


@pytest.mark.parametrize("body, said", [
    ("- id: a\n  expected_document: x.md\n", "a: 'question' is required"),
    ("- question: x\n", "question 1: 'id' is required"),
    ("- id: a\n  question: x\n  expected_pages: [three]\n", "a: expected_pages must be page numbers"),
    ("- id: a\n  question: x\n- id: a\n  question: y\n", "a: the id is used twice"),
    ("id: a\n", "a YAML list"),
])
def test_qset_2_a_malformed_set_is_refused_naming_the_question(tmp_path, body, said):
    from axiom.rag.eval import load_questions

    with pytest.raises(ValueError, match=re.escape(said)):
        load_questions(_write(tmp_path, "q.yaml", body))


def test_qset_2_the_cli_reads_a_set_from_any_path_and_says_when_it_is_missing(tmp_path, capsys):
    from axiom.rag import cli as rag_cli

    with pytest.raises(SystemExit) as gone:
        rag_cli.main(["eval", "--questions", str(tmp_path / "absent.yaml"), "--retrieval-only"])
    assert gone.value.code == 1
    assert "questions file not found" in capsys.readouterr().err

    bad = _write(tmp_path, "bad.yaml", "- id: a\n")
    with pytest.raises(SystemExit) as refused:
        rag_cli.main(["eval", "--questions", str(bad), "--retrieval-only"])
    assert refused.value.code == 2
    assert "a: 'question' is required" in capsys.readouterr().err


# -- QSET-3 ------------------------------------------------------------------


def test_qset_3_a_retrieval_run_reports_per_intent():
    from axiom.rag.eval import Citation, RagEvalQuestion, run_retrieval_eval

    def hits(q):
        return {"one": [Citation(source_path="a.md")],
                "two": [Citation(source_path="x.md"), Citation(source_path="b.md")],
                "three": [Citation(source_path="x.md")]}[q]

    qs = [RagEvalQuestion(id="1", question="one", expected_sources=["a.md"], intent="lookup"),
          RagEvalQuestion(id="2", question="two", expected_sources=["b.md"], intent="lookup"),
          RagEvalQuestion(id="3", question="three", expected_sources=["c.md"], intent="synthesis")]
    report = run_retrieval_eval(qs, retriever_fn=hits, k=8)

    assert report.by_intent["lookup"].scored == 2
    assert report.by_intent["lookup"].recall_at_k == pytest.approx(1.0)
    assert report.by_intent["lookup"].mrr == pytest.approx(0.75)
    assert report.by_intent["synthesis"].recall_at_k == 0.0


# -- QSET-4 ------------------------------------------------------------------


def test_qset_4_the_public_sample_is_small_complete_and_over_its_own_documents():
    from axiom.rag import eval_sample
    from axiom.rag.eval import load_questions

    qs = load_questions(eval_sample.QUESTIONS)
    assert 5 <= len(qs) <= 10
    assert all(q.intent for q in qs), "every sample question names its intent"
    docs = eval_sample.documents()
    names = {d["title"].lower() for d in docs} | {d["path"].lower() for d in docs}
    for q in qs:
        for want in q.expected_sources:
            assert any(n == want.lower() or n.endswith("/" + want.lower()) for n in names), (q.id, want)
    assert any(q.expected_behavior == "abstain" for q in qs), "one question the reader may not answer"


# -- QSET-5 ------------------------------------------------------------------


def test_qset_5_the_public_sample_runs_as_a_site_runs_its_set(monkeypatch, capsys, sample_store):  # noqa: F811
    import axiom.rag.embeddings as emb
    from axiom.rag import cli as rag_cli
    from axiom.rag import eval_sample

    monkeypatch.setattr(emb, "embed_texts_with_model", _embed)
    monkeypatch.delenv("AXIOM_RAG_RANKING", raising=False)
    monkeypatch.setattr("axiom.rag.cli._get_store", lambda: sample_store)

    rag_cli.main(["eval", "--questions", str(eval_sample.QUESTIONS), "--retrieval-only",
                  "--rerank", "both", "--k", "8"])

    out = capsys.readouterr().out
    assert "=== retrieval: rerank on ===" in out and "=== retrieval: rerank off ===" in out
    assert "scored questions: 7" in out  # the abstain question names nothing to find
    recall = [float(x) for x in re.findall(r"recall@8: ([0-9.]+)", out)]
    assert recall[:2] == [1.0, 1.0], out  # the sample's documents are all found within 8
    assert "intent lookup" in out and "intent synthesis" in out


def test_qset_5_the_sample_file_is_the_documented_format():
    """The sample is the format example: only the documented fields."""
    from axiom.rag import eval_sample

    documented = {"id", "question", "expected_document", "expected_pages", "intent", "expected_behavior"}
    for row in yaml.safe_load(eval_sample.QUESTIONS.read_text(encoding="utf-8")):
        assert set(row) <= documented, row
