# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An ADR number is an identifier, so exactly one document may hold it.

Three failures of this in a single day (2026-09-28) prompted the check:

- two files both named ``adr-005-*`` in a consumer repo
- two parallel branches each cutting ``adr-137``, neither able to see the
  other
- two files here whose heading disagreed with their own filename, so one
  document answered to two numbers and one number named two documents

The three are the same defect wearing different clothes, and none of them
surfaces until somebody follows a citation and lands on the wrong page.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from lint_adr_numbers import (  # noqa: E402
    collisions,
    find_adrs,
    lint,
    title_mismatches,
)

ADRS = REPO / "docs" / "adrs"


def test_there_are_adrs_to_check():
    """A guard over an empty directory passes forever."""
    assert len(find_adrs(ADRS)) > 50


def test_no_two_documents_share_a_number():
    clashing = collisions(find_adrs(ADRS))

    assert not clashing, "\n".join(
        f"ADR-{n:03d}: " + ", ".join(p.name for p in paths) for n, paths in clashing.items()
    )


def test_no_document_answers_to_two_numbers():
    """The quiet one: the file sorts correctly and the index looks right."""
    bad = title_mismatches(find_adrs(ADRS))

    assert not bad, "\n".join(
        f"{p.name} is headed ADR-{titled:03d}" for p, filed, titled in bad
    )


def test_the_lint_passes_over_the_real_tree():
    assert lint(ADRS) == 0


def test_a_mismatched_heading_is_caught(tmp_path):
    """Negative control — the check must be able to fail."""
    (tmp_path / "adr-007-a-thing.md").write_text("# ADR-009 — a thing\n")

    assert title_mismatches(find_adrs(tmp_path))


def test_a_matching_heading_passes(tmp_path):
    (tmp_path / "adr-007-a-thing.md").write_text("# ADR-007 — a thing\n")

    assert not title_mismatches(find_adrs(tmp_path))


def test_a_document_with_no_adr_heading_is_not_flagged(tmp_path):
    """Some ADRs lead with front matter or prose; absence is not a mismatch."""
    (tmp_path / "adr-007-a-thing.md").write_text("Status: Accepted\n\n# A thing\n")

    assert not title_mismatches(find_adrs(tmp_path))


def test_the_next_number_consults_more_than_the_local_tree():
    """Two branches cut from one main are both told the same next number
    unless something looks wider than the working copy."""
    source = (REPO / "scripts" / "lint_adr_numbers.py").read_text()

    assert "remote_adr_numbers" in source
