# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression net for the question banks that ship in the extension.

Small but load-bearing: these banks let an instructor (or CI) run
`axi classroom evals <class> --bank <shipped-bank>` day-one without
hand-crafting a JSONL. If we break the load path or the keyword
format, this test fires before a user hits the error at Prague.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.classroom.classroom_evals import load_bank

BANKS_DIR = Path(__file__).parent.parent / "banks"


def test_banks_dir_exists():
    assert BANKS_DIR.is_dir(), f"{BANKS_DIR} must exist"


def test_ne101_core_bank_loads():
    bank = load_bank(BANKS_DIR / "ne101_core.jsonl")
    assert len(bank.questions) >= 10, (
        "NE101 bank should have at least 10 core questions"
    )


def test_ne101_questions_all_have_keywords():
    bank = load_bank(BANKS_DIR / "ne101_core.jsonl")
    for q in bank.questions:
        assert q.expected_keywords, (
            f"question {q.question!r} has no expected_keywords"
        )
        for kw in q.expected_keywords:
            assert isinstance(kw, str) and kw, (
                f"{q.question!r}: empty/non-string keyword {kw!r}"
            )


def test_ne101_categories_are_plausible():
    bank = load_bank(BANKS_DIR / "ne101_core.jsonl")
    categories = {q.category for q in bank.questions if q.category}
    # At least some questions categorized; no enforcement of a closed
    # set so new categories don't break the test.
    assert categories, "expected at least one categorized question"
