# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi rag add <file>`` — single-file manual ingest.

Built 2026-06-01 in response to Ben's operator workflow request:
    "we need a way to add a single document manually"

Wraps the existing ``ingest_file`` codepath (which already handles
checksum-based dedup, EC screening, chunking, and embedding) with a
single-file CLI verb. Adds:

- ``--source-path`` override so a file copied locally from Box/GitHub
  records its origin path, not the local path.
- ``--corpus`` selection.

Dedup behavior is the existing same-path-same-checksum no-op from
``ingest_file``. Cross-path content dedup (same MD5, different paths)
is tracked in the medallion epic as silver-tier work; the
``documents.content_hash`` column is already populated for it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# A pin-down test double — we don't recreate ingest_file's full
# behavior in unit tests; we verify the CLI calls into it correctly.

class _AddStore:
    def __init__(self):
        self.documents: dict[tuple[str, str], dict] = {}
        self.upserts: list[dict] = []

    def get_document(self, source_path, corpus):
        return self.documents.get((source_path, corpus))

    def upsert_chunks(self, **kwargs):
        self.upserts.append(kwargs)

    def list_document_paths(self, corpus):
        return [p for (p, c) in self.documents if c == corpus]

    def close(self):
        pass


def _run(argv, store):
    from axiom.rag import cli as rag_cli

    with patch("axiom.rag.cli._get_store", return_value=store):
        return rag_cli.main(argv)


# -- happy path --------------------------------------------------------------


def test_add_indexes_a_file(tmp_path, capsys):
    f = tmp_path / "test.md"
    f.write_text("# Test\n\nSome content for testing.\n")
    store = _AddStore()

    with patch("axiom.rag.cli._ingest_one_file") as ingest:
        ingest.return_value = (1, 3)   # (files_indexed, chunks_created)
        _run(["add", str(f), "--corpus", "rag-internal"], store)

    assert ingest.called
    out = capsys.readouterr().out
    assert "Indexed" in out
    assert "3" in out  # chunks reported


def test_add_nonexistent_file_exits_nonzero(capsys):
    store = _AddStore()
    with pytest.raises(SystemExit) as ei:
        _run(["add", "/no/such/file.md", "--corpus", "rag-internal"], store)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err.lower() or "does not exist" in err.lower()


def test_add_source_path_override_passed_through(tmp_path):
    f = tmp_path / "local-copy.md"
    f.write_text("content\n")
    store = _AddStore()
    captured = {}

    def fake_ingest(path, store, *, source_path=None, corpus="rag-internal",
                    chunking_tier="fixed"):
        captured["path"] = path
        captured["source_path"] = source_path
        captured["corpus"] = corpus
        return (1, 1)

    with patch("axiom.rag.cli._ingest_one_file", side_effect=fake_ingest):
        _run(["add", str(f), "--source-path", "box://CRISP/the-real-path.pdf",
              "--corpus", "rag-internal"], store)

    assert captured["source_path"] == "box://CRISP/the-real-path.pdf"
    assert captured["corpus"] == "rag-internal"


def test_add_default_corpus_is_rag_internal(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("y\n")
    store = _AddStore()
    seen = {}

    def fake_ingest(path, store, *, source_path=None, corpus="rag-internal",
                    chunking_tier="fixed"):
        seen["corpus"] = corpus
        return (1, 1)

    with patch("axiom.rag.cli._ingest_one_file", side_effect=fake_ingest):
        _run(["add", str(f)], store)
    assert seen["corpus"] == "rag-internal"


def test_add_explicit_corpus_propagates(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("y\n")
    store = _AddStore()
    seen = {}

    def fake_ingest(path, store, *, source_path=None, corpus="rag-internal",
                    chunking_tier="fixed"):
        seen["corpus"] = corpus
        return (1, 1)

    with patch("axiom.rag.cli._ingest_one_file", side_effect=fake_ingest):
        _run(["add", str(f), "--corpus", "rag-org"], store)
    assert seen["corpus"] == "rag-org"
