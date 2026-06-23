# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi rag remove <name-or-path>`` — one-shot document removal.

Built 2026-06-01 in response to a Shayan operator request:
    "Remove 1946 - CP2 Layer Details.pdf from the RAG"

Today's only path is ``axi rag audit --purge`` with a rule file — too
heavy for "just delete this one file." This verb closes the gap.

Resolution rules:
- If the input contains a ``/``, treat as an exact source_path match.
- Otherwise, basename match against ``source_path LIKE '%/<name>'``
  OR equality (the path may BE a bare filename).
- 0 matches: exit 1 with a "not found" message.
- 1 match: show "Would delete <path> (N chunks)" — require ``--yes``
  to actually delete; without it, exit 0 (dry-run preview).
- N matches: show all candidates with chunk counts; require either
  the user to re-run with the full ``--path`` OR pass ``--all``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class _RemoveStore:
    """Test double honoring the read + delete surface ``cmd_remove`` uses."""

    def __init__(self, documents: dict[str, int]):
        # documents: { source_path: chunk_count }
        self._docs = dict(documents)
        self.deleted: list[str] = []

    def find_documents_by_name(self, name: str, corpus: str) -> list[dict]:
        # Mirror the real store: exact path OR basename match.
        if "/" in name:
            if name in self._docs:
                return [{"source_path": name, "chunk_count": self._docs[name]}]
            return []
        out = []
        for p, n in self._docs.items():
            if p == name or p.endswith("/" + name):
                out.append({"source_path": p, "chunk_count": n})
        return out

    def delete_document(self, path: str, corpus: str) -> None:
        self.deleted.append(path)
        self._docs.pop(path, None)

    def close(self):
        pass


def _run(argv: list[str], store: _RemoveStore):
    from axiom.rag import cli as rag_cli

    with patch("axiom.rag.cli._get_store", return_value=store):
        return rag_cli.main(argv)


# -- single-match path --------------------------------------------------------


def test_remove_single_basename_match_dry_run_does_not_delete(capsys):
    store = _RemoveStore({"box/CRISP Literature/1946 - CP2 Layer Details.pdf": 47})
    _run(["remove", "1946 - CP2 Layer Details.pdf", "--corpus", "rag-internal"], store)
    out = capsys.readouterr().out
    assert "1946 - CP2 Layer Details.pdf" in out
    assert "47" in out                        # chunk count surfaced
    assert "--yes" in out                      # nudge to confirm
    assert store.deleted == []                 # NO deletion without --yes


def test_remove_single_basename_match_with_yes_deletes(capsys):
    store = _RemoveStore({"box/CRISP Literature/1946 - CP2 Layer Details.pdf": 47})
    _run(["remove", "1946 - CP2 Layer Details.pdf", "--corpus", "rag-internal", "--yes"], store)
    assert store.deleted == ["box/CRISP Literature/1946 - CP2 Layer Details.pdf"]
    out = capsys.readouterr().out
    assert "Removed" in out
    assert "47" in out                         # honest chunks-removed report


def test_remove_exact_path_match_with_yes_deletes(capsys):
    full = "box/CRISP Literature/1946 - CP2 Layer Details.pdf"
    store = _RemoveStore({full: 12})
    _run(["remove", full, "--corpus", "rag-internal", "--yes"], store)
    assert store.deleted == [full]


# -- zero-match path ----------------------------------------------------------


def test_remove_no_match_exits_nonzero(capsys):
    store = _RemoveStore({"box/other.pdf": 3})
    with pytest.raises(SystemExit) as ei:
        _run(["remove", "nothing-like-this.pdf", "--corpus", "rag-internal"], store)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err.lower()
    assert store.deleted == []


# -- multi-match path ---------------------------------------------------------


def test_remove_multiple_matches_requires_disambiguation(capsys):
    store = _RemoveStore({
        "box/folder-a/1946 - CP2 Layer Details.pdf": 30,
        "box/folder-b/1946 - CP2 Layer Details.pdf": 17,
    })
    with pytest.raises(SystemExit) as ei:
        _run(["remove", "1946 - CP2 Layer Details.pdf",
              "--corpus", "rag-internal", "--yes"], store)
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err.lower() or "multiple" in err.lower()
    # All candidates must be listed so operator can pick
    assert "folder-a" in err
    assert "folder-b" in err
    assert "--all" in err                       # nudge mentions the escape hatch
    assert store.deleted == []                   # NOTHING deleted on ambiguity


def test_remove_multiple_matches_with_all_deletes_each(capsys):
    store = _RemoveStore({
        "box/folder-a/1946 - CP2 Layer Details.pdf": 30,
        "box/folder-b/1946 - CP2 Layer Details.pdf": 17,
    })
    _run(["remove", "1946 - CP2 Layer Details.pdf",
          "--corpus", "rag-internal", "--all", "--yes"], store)
    assert sorted(store.deleted) == [
        "box/folder-a/1946 - CP2 Layer Details.pdf",
        "box/folder-b/1946 - CP2 Layer Details.pdf",
    ]
    out = capsys.readouterr().out
    assert "47" in out  # 30 + 17, total chunks honestly reported


# -- corpus default + propagation --------------------------------------------


def test_remove_corpus_argument_is_honored(capsys):
    """The corpus arg must reach the store call; default should be rag-internal."""
    seen_corpora: list[str] = []

    class _CapturingStore(_RemoveStore):
        def find_documents_by_name(self, name, corpus):
            seen_corpora.append(corpus)
            return super().find_documents_by_name(name, corpus)
        def delete_document(self, path, corpus):
            seen_corpora.append(corpus)
            super().delete_document(path, corpus)

    store = _CapturingStore({"a.pdf": 1})
    _run(["remove", "a.pdf", "--corpus", "rag-org", "--yes"], store)
    assert seen_corpora == ["rag-org", "rag-org"]
