# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MemoryBronzeReceiptStore — protocol-compliance tests.

Mirrors the DuckDB store's contract tests. Both backends must satisfy the
same axiom.medallion.receipts.BronzeReceiptStore protocol so consumers can
swap them transparently.
"""

from __future__ import annotations

import pytest

from axiom.medallion.receipts import BronzeReceiptStore
from axiom_ext_data_platform import MemoryBronzeReceiptStore


@pytest.fixture
def store():
    return MemoryBronzeReceiptStore()


def test_satisfies_protocol(store):
    assert isinstance(store, BronzeReceiptStore)


def test_write_and_lookup_compute_receipt(store):
    receipt = {
        "uri": "axiom://compute/sha256:e5f6abc",
        "kernel": "openmc",
        "value_summary": {"k_eff": 1.0042},
    }
    store.write_compute_receipt(receipt)
    found = store.lookup("axiom://compute/sha256:e5f6abc")
    assert found is not None
    assert found["value_summary"]["k_eff"] == pytest.approx(1.0042)


def test_lookup_missing_returns_none(store):
    assert store.lookup("axiom://compute/sha256:missing") is None


def test_iter_returns_all(store):
    for i in range(4):
        store.write_compute_receipt({"uri": f"axiom://compute/sha256:r{i}"})
    assert len(list(store.iter_compute_receipts())) == 4


def test_agreement_receipt_separate(store):
    store.write_agreement_receipt({
        "uri": "axiom://agree/sha256:f7g8",
        "axis_emitted_for": "A3",
    })
    found = store.lookup("axiom://agree/sha256:f7g8")
    assert found is not None
    assert found["axis_emitted_for"] == "A3"
    # And it's not in compute_receipts
    assert list(store.iter_compute_receipts()) == []
    assert len(list(store.iter_agreement_receipts())) == 1
