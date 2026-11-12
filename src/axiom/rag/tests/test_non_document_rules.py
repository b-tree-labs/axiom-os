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
