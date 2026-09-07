# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ingest skip categorization + drop reporting.

These cover the contract that a bulk onboarding of a large, heterogeneous
corpus (e.g. a Box knowledge dump) must be *honest* about what entered the
index versus what was dropped and why — distinguishing "unchanged" (already
indexed) from "unsupported" (no extractor) from "failed" (supported type but
no text extracted). Without this split, ~half of a real facility corpus
drops silently and the operator can't tell.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from axiom.rag.ingest import IngestStats, ingest_file, ingest_path
from axiom.rag.ingest_router import Disposition, ProvenanceRule


class _FakeStore:
    """Minimal RAGStore stand-in for the skip paths (no DB / network)."""

    def __init__(self, existing: dict | None = None):
        self._existing = existing or {}
        self.upserts = 0
        self.last_corpus: str | None = None

    def get_document(self, rel_path: str):
        return self._existing.get(rel_path)

    def upsert_chunks(self, *a, **k):
        self.upserts += 1
        self.last_corpus = k.get("corpus")

    def close(self):
        pass


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# -- IngestStats unit contract ------------------------------------------------


def test_files_skipped_sums_all_skip_kinds():
    s = IngestStats(files_unchanged=2, files_unsupported=3, files_failed=1)
    assert s.files_skipped == 6  # backward-compatible total


def test_iadd_merges_counts_and_ext_histogram():
    a = IngestStats(files_indexed=1, files_unsupported=1, unsupported_by_ext={".cnf": 1})
    b = IngestStats(files_indexed=2, files_unsupported=2, unsupported_by_ext={".cnf": 1, ".jpg": 2})
    a += b
    assert a.files_indexed == 3
    assert a.files_unsupported == 3
    assert a.unsupported_by_ext == {".cnf": 2, ".jpg": 2}


def test_drop_report_empty_when_only_unchanged():
    assert IngestStats(files_unchanged=5).drop_report() == ""


def test_drop_report_lists_unsupported_by_ext_most_common_first():
    s = IngestStats(files_unsupported=3, unsupported_by_ext={".jpg": 1, ".cnf": 2})
    report = s.drop_report()
    assert ".cnf=2" in report and ".jpg=1" in report
    assert report.index(".cnf") < report.index(".jpg")


# -- ingest_file categorization -----------------------------------------------


def test_unsupported_extension_is_counted_not_silently_dropped(tmp_path):
    f = tmp_path / "spectrum.cnf"
    f.write_bytes(b"\x00\x01binary")
    store = _FakeStore()
    stats = ingest_file(f, store, repo_root=tmp_path)
    assert stats.files_unsupported == 1
    assert stats.unsupported_by_ext == {".cnf": 1}
    assert stats.files_indexed == 0
    assert store.upserts == 0  # returned before touching the store


def test_unchanged_checksum_is_counted_as_unchanged(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# hello\n\nsome content")
    store = _FakeStore(existing={"note.md": {"checksum": _md5(f)}})
    stats = ingest_file(f, store, repo_root=tmp_path)
    assert stats.files_unchanged == 1
    assert stats.files_unsupported == 0
    assert store.upserts == 0


# -- directory walk now sees unsupported files --------------------------------


def test_ingest_path_walks_all_files_and_categorizes_drops(tmp_path):
    # The old per-extension glob would never have *seen* these.
    (tmp_path / "a.cnf").write_bytes(b"x")
    (tmp_path / "b.cnf").write_bytes(b"y")
    (tmp_path / "c.spc").write_bytes(b"z")
    (tmp_path / "pic.jpg").write_bytes(b"\xff\xd8")
    store = _FakeStore()
    stats = ingest_path(tmp_path, store)
    assert stats.files_unsupported == 4
    assert stats.unsupported_by_ext == {".cnf": 2, ".spc": 1, ".jpg": 1}
    assert stats.files_indexed == 0


def test_ingest_path_skips_hidden_and_macosx(tmp_path):
    (tmp_path / ".hidden.cnf").write_bytes(b"x")
    macosx = tmp_path / "__MACOSX"
    macosx.mkdir()
    (macosx / "junk.cnf").write_bytes(b"y")
    store = _FakeStore()
    stats = ingest_path(tmp_path, store)
    assert stats.files_unsupported == 0


# -- CLI wiring: drops are surfaced to the operator ---------------------------


def test_cli_index_prints_drop_report(tmp_path, capsys):
    """`axi rag index <dir>` must tell the operator what it dropped and why.

    In-process (patched store) rather than a subprocess because the CLI's
    _get_store() currently hardwires the Postgres backend; this still drives
    real arg-parsing → cmd_index → ingest_path → the drop-report print.
    """
    from unittest.mock import patch

    from axiom.rag import cli as rag_cli

    (tmp_path / "a.cnf").write_bytes(b"x")
    (tmp_path / "b.cnf").write_bytes(b"y")
    (tmp_path / "pic.jpg").write_bytes(b"\xff\xd8")

    store = _FakeStore()
    with patch("axiom.rag.cli._get_store", return_value=store):
        rag_cli.main(["index", str(tmp_path)])

    err = capsys.readouterr().err
    assert "Dropped (not indexed)" in err
    assert ".cnf=2" in err
    assert ".jpg=1" in err


# -- durability: a transient embedding failure must not silently commit text-only --


def test_embedding_failure_does_not_commit_or_record_checksum(tmp_path, monkeypatch):
    """A network drop to the embedder must NOT leave the doc committed text-only.

    Committing on failure would store the doc without vectors AND record its
    checksum, so a later run would treat it as 'unchanged' and never embed it.
    Instead it must count as failed and not touch the store, so a re-run retries.
    """
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.embeddings import EmbeddingError

    def _boom(texts):
        raise EmbeddingError("network drop to embedder")

    monkeypatch.setattr(ingest_mod, "embed_texts", _boom)
    f = tmp_path / "big.md"
    f.write_text("# Doc\n\n" + "content paragraph. " * 40)
    store = _FakeStore()  # get_document → None (new file)
    stats = ingest_file(f, store, repo_root=tmp_path)

    assert stats.files_failed == 1
    assert stats.files_indexed == 0
    assert store.upserts == 0  # not committed → re-run retries, not skip-as-unchanged


def test_no_provider_still_indexes_text_only(tmp_path, monkeypatch):
    """When NO provider is configured (embed_texts returns None), text-only is fine."""
    from axiom.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts: None)
    f = tmp_path / "note.md"
    f.write_text("# Doc\n\nsome content here for chunking purposes, enough to chunk.")
    store = _FakeStore()
    stats = ingest_file(f, store, repo_root=tmp_path)

    assert stats.files_indexed == 1
    assert store.upserts == 1  # legitimate text-only commit


# -- provenance routing wired into the ingest path ----------------------------


def test_ingest_file_excludes_by_provenance(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    f = vendor / "manual.md"
    f.write_text("# manual\n\nlicensed vendor content")
    store = _FakeStore()
    rules = [ProvenanceRule("vendor/", Disposition.EXCLUDE, reason="licensed vendor")]
    stats = ingest_file(f, store, repo_root=tmp_path, rules=rules)
    assert stats.files_excluded == 1
    assert stats.files_indexed == 0
    assert store.upserts == 0  # controlled source never read or embedded
    assert stats.excluded_by_rule == {"vendor/": 1}


def test_ingest_file_quarantines_archive_before_unsupported_check(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    f = data / "bundle.zip"
    f.write_bytes(b"PK\x03\x04")
    store = _FakeStore()
    rules = [ProvenanceRule("*.zip", Disposition.QUARANTINE, reason="archive")]
    stats = ingest_file(f, store, repo_root=tmp_path, rules=rules)
    assert stats.files_quarantined == 1
    assert stats.files_unsupported == 0  # provenance gate precedes the extension check
    assert store.upserts == 0


def test_ingest_file_allow_routes_to_resolved_tier(tmp_path, monkeypatch):
    from axiom.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts: None)
    pub = tmp_path / "public"
    pub.mkdir()
    f = pub / "intro.md"
    f.write_text("# Intro\n\nsome public content to chunk and index")
    store = _FakeStore()
    rules = [ProvenanceRule("public/", Disposition.ALLOW, tier="rag-community")]
    stats = ingest_file(f, store, repo_root=tmp_path, corpus="rag-internal", rules=rules)
    assert stats.files_indexed == 1
    assert store.last_corpus == "rag-community"  # routed to the rule's tier


def test_ingest_path_applies_rules(tmp_path, monkeypatch):
    from axiom.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts: None)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "m.md").write_text("x" * 50)
    (tmp_path / "ok.md").write_text("# title\n\ncontent to index")
    store = _FakeStore()
    rules = [ProvenanceRule("vendor/", Disposition.EXCLUDE)]
    stats = ingest_path(tmp_path, store, corpus="rag-internal", rules=rules)
    assert stats.files_excluded == 1
    assert stats.files_indexed == 1  # ok.md still ingested
