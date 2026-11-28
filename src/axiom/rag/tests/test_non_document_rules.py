# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Paths that are not documents in any domain are excluded before ingest.

Found on a production node 2026-09-26: vendored ``numpy`` and ``pip``
``LICENSE.txt`` files had been indexed as documents, and a TriBITS build-system
guide contributed 858 chunks to a corpus of reactor literature. Nothing stopped
them, so they would return on the next pass.

The second half of this file matters more than the first: a guard that refuses
documents is worse than no guard, because it quietly empties the corpus.
"""

from __future__ import annotations

import pytest

from axiom.rag.ingest_router import (
    NON_DOCUMENT_PATTERNS,
    Disposition,
    ProvenanceRule,
    non_document_rules,
    route_path,
    with_non_document_rules,
)

NOT_DOCUMENTS = [
    ".venv/lib/python3.14/site-packages/numpy-2.3.5.dist-info/LICENSE.txt",
    "netl_pxi/.venv/lib/python3.14/site-packages/pip/_vendor/distlib/LICENSE.txt",
    "cmake/tribits/doc/guides/TribitsGuidesBody.txt",
    "project/node_modules/left-pad/readme.md",
    "src/__pycache__/reactor.cpython-314.pyc",
    "build/CMakeFiles/link.txt",
    "CMakeCache.txt",
    "repo/.git/COMMIT_EDITMSG",
    "repo/.mypy_cache/3.14/reactor.json",
    "poetry.lock",
    "package-lock.json",
    "lib/libcore.so",
    "notes/.ipynb_checkpoints/analysis-checkpoint.ipynb",
    "docs/.DS_Store",
    # #2101: what a connector that indexed repository checkouts wholesale brought in.
    "venv/lib/python3.12/site-packages/requests/README.md",
    "netl_pxi/venv/pyvenv.cfg",
    "site-packages/pip/_vendor/distlib/LICENSE.txt",
    "wheels/numpy-2.3.5.dist-info/LICENSE.txt",
    "numpy-2.3.5.dist-info/METADATA",
    "src/reactor.egg-info/SOURCES.txt",
    "reactor.egg-info/top_level.txt",
    ".pytest_cache/README.md",
    "repo/.pytest_cache/README.md",
    ".mypy_cache/3.14/reactor.json",
    ".ruff_cache/CACHEDIR.TAG",
    ".tox/py314/log/1-commands.log",
    "repo/.cache/pip/http/index.txt",
    ".cache/huggingface/README.md",
    "docs/_build/html/index.txt",
    "_build/latex/manual.txt",
    "CMakeFiles/link.txt",
    "CMakeLists.txt",
    "scale/packages/Origen/CMakeLists.txt",
    "cmake/modules/FindHDF5.cmake",
]

#: Real documents from the corpus this guard was written against. Every one must
#: survive: these are the answers the platform exists to retrieve.
ARE_DOCUMENTS = [
    "/_____Literature_____/MSR/MSRE and MSBR Documents/1970 Haubenreich - ORNL-CF-70-2-7.pdf",
    "pdf/ORNL-5018/ORNL-5018/hybrid_ocr/ORNL-5018.md",
    "source/methods/neutron_physics.txt",
    "docs/source/progression_problems/TRIGA/ANDRETTI/system_specifications.txt",
    "01-11-2017 Prestart.txt",
    "kb/wiki/timelines/operations.md",
    "/! Operations/! Operations Records/2022/Console Log Files/Console Files/01-05-2012 Prestart.txt",
    "tech-specs/spec-logging.md",
    "requirements/prd-rag.md",
    "Rx O&M Manuals and logs scanned/TRIGA Manual.pdf",
    "vendor-quotes/2026 quotation.pdf",
    "build-notes/core-rebuild-2019.md",
    "doc/operator-training.md",
    # Near misses of the #2101 additions: none of these is a tooling directory or a build script.
    "venv-notes/setup.md",
    "inventory/venvs.md",
    "cache/summary.md",
    "procedures/cache-flush.md",
    "build/core-rebuild.md",
    "builds/_build-notes.md",
    "cmake-guide.md",
    "docs/CMakeLists-explained.md",
    "packages/list.txt",
    "dist-info.md",
]


@pytest.mark.parametrize("path", NOT_DOCUMENTS)
def test_tooling_artifacts_are_excluded(path):
    decision = route_path(path, non_document_rules())
    assert decision.disposition is Disposition.EXCLUDE, path
    assert decision.reason.startswith("not a document: "), decision.reason


@pytest.mark.parametrize("path", ARE_DOCUMENTS)
def test_real_documents_are_never_excluded(path):
    """Including the near misses on purpose: `vendor-quotes/`, `build-notes/`
    and `doc/` look like tooling and are not, which is why no pattern names a
    bare `vendor/`, `build/` or `doc/` directory."""
    assert route_path(path, non_document_rules()).disposition is Disposition.ALLOW, path


def test_a_consumer_rule_can_override_a_platform_exclude():
    """First match wins, and the consumer's rules come first: a corpus where a
    site-packages path IS the document can say so."""
    consumer = [
        ProvenanceRule(
            pattern="*/site-packages/our-vendored-spec.md",
            disposition=Disposition.ALLOW,
            tier="rag-org",
            reason="we ship this spec inside the package",
        )
    ]
    rules = with_non_document_rules(consumer)

    kept = route_path("x/site-packages/our-vendored-spec.md", rules)
    assert kept.disposition is Disposition.ALLOW and kept.tier == "rag-org"

    dropped = route_path("x/site-packages/numpy/LICENSE.txt", rules)
    assert dropped.disposition is Disposition.EXCLUDE


def test_with_non_document_rules_keeps_consumer_rules_first_and_adds_nothing_else():
    consumer = [ProvenanceRule(pattern="secret/", disposition=Disposition.QUARANTINE)]
    merged = with_non_document_rules(consumer)
    assert merged[0] is consumer[0]
    assert len(merged) == 1 + len(NON_DOCUMENT_PATTERNS)
    assert all(r.disposition is Disposition.EXCLUDE for r in merged[1:])


def test_every_pattern_carries_a_reason_a_human_can_check():
    for pattern, reason in NON_DOCUMENT_PATTERNS:
        assert pattern and reason, pattern
        assert not reason.endswith("."), reason


# ---- one list, applied by every ingest path and by the corpus copy (#2101) ------------------------


def test_non_document_reason_names_why_or_says_none():
    from axiom.rag.ingest_router import non_document_reason

    assert (
        non_document_reason("repo/.venv/lib/site.py")
        == "not a document: Python virtual environment"
    )
    assert non_document_reason("kb/wiki/timelines/operations.md") is None


def test_rag_ingest_applies_the_default_ignore_list_without_being_given_rules(
    tmp_path, monkeypatch
):
    """``axi rag index`` / ``ingest`` (``ingest_path``) had no default ignore: a virtualenv that is
    not hidden (``venv/``), a ``site-packages`` tree or a build tree under the path was indexed."""
    from unittest.mock import MagicMock

    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest import ingest_path

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts, **k: None)
    for rel in (
        "venv/lib/site-packages/pkg/README.md",
        "docs/_build/index.md",
        "src/CMakeLists.txt",
        "third/pkg-1.0.dist-info/LICENSE.txt",
        "notes/operations.md",
    ):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            "# A heading\n\nSome prose that would be a document if it were one.\n", encoding="utf-8"
        )
    store = MagicMock()
    store.get_document.return_value = None
    seen = []
    store.upsert_chunks.side_effect = lambda chunks, *a, **k: seen.append(chunks[0].source_path)

    stats = ingest_path(tmp_path, store, corpus="rag-internal")

    assert stats.files_excluded == 4
    assert seen in ([], ["notes/operations.md"]), "nothing ignored reaches the store"
    assert all(
        rule.startswith(("venv/", "*/_build/", "*/CMakeLists", "*.dist-info"))
        or "site-packages" in rule
        for rule in stats.excluded_by_rule
    )


def test_a_callers_rule_still_wins_over_the_default_ignore_list(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest import ingest_file

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts, **k: None)

    f = tmp_path / "site-packages" / "our-vendored-spec.md"
    f.parent.mkdir()
    f.write_text("# Spec\n\nWe ship this specification inside the package.\n", encoding="utf-8")
    store = MagicMock()
    store.get_document.return_value = None
    keep = [
        ProvenanceRule(pattern="site-packages/our-vendored-spec.md", disposition=Disposition.ALLOW)
    ]

    assert ingest_file(f, store, repo_root=tmp_path).files_excluded == 1
    assert ingest_file(f, store, repo_root=tmp_path, rules=keep).files_excluded == 0


def test_the_connector_writers_and_the_corpus_copy_use_this_list():
    """One list: the connector path wraps its rules with it, and re-embed asks it by path."""
    import inspect

    from axiom.extensions.builtins.data_platform import ingest_sink
    from axiom.extensions.builtins.data_platform.agents.plinth.skills import run_ingest
    from axiom.rag import re_embed
    from axiom.rag.ingest_router import non_document_reason

    assert "with_non_document_rules(" in inspect.getsource(ingest_sink.build_writer_for_config)
    assert "with_non_document_rules(" in inspect.getsource(run_ingest._build_writer)
    assert re_embed.exclusion_reason("x/venv/lib/a.py", []) == (
        "default",
        non_document_reason("x/venv/lib/a.py"),
    )
    assert re_embed.exclusion_reason("ops/log.md", []) is None
