# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DuckDBBronzeReceiptStore — protocol-compliance + DuckDB-specific SQL tests.

Phase 6a of the Twin OS Build March. Same BronzeReceiptStore protocol as
the JsonlBronzeReceiptStore (which stays in the twin extension as the
zero-dep dev fallback) and the MemoryBronzeReceiptStore. Phase 6b will
add IcebergBronzeReceiptStore for production volume; the protocol
contract is invariant so consumer code is unchanged across the swap.
"""

from __future__ import annotations

import pytest

from axiom.medallion.receipts import BronzeReceiptStore
from axiom_ext_data_platform import DuckDBBronzeReceiptStore


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "bronze.duckdb"
    s = DuckDBBronzeReceiptStore(db_path=db_path)
    yield s
    s.close()


# ----- Protocol-compliance tests -----


def test_satisfies_protocol(store):
    assert isinstance(store, BronzeReceiptStore)


def test_write_compute_receipt(store):
    receipt = {
        "uri": "axiom://compute/sha256:e5f6abc",
        "kernel": "openmc",
        "executing_peer_id": "example-host",
        "value_summary": {"k_eff": 1.0042, "k_eff_std": 0.00012},
    }
    store.write_compute_receipt(receipt)
    found = store.lookup("axiom://compute/sha256:e5f6abc")
    assert found is not None
    assert found["uri"] == receipt["uri"]
    assert found["value_summary"]["k_eff"] == pytest.approx(1.0042)


def test_multiple_writes_accumulate(store):
    for i in range(5):
        store.write_compute_receipt({
            "uri": f"axiom://compute/sha256:r{i}",
            "value_summary": {"k_eff": 1.0 + 0.001 * i},
        })
    receipts = list(store.iter_compute_receipts())
    assert len(receipts) == 5


def test_write_agreement_receipt_separate_table(store):
    store.write_agreement_receipt({
        "uri": "axiom://agree/sha256:f7g8",
        "axis_emitted_for": "A3",
        "subject_receipt_uri": "axiom://compute/sha256:e5f6",
        "delta_value": -12.3,
        "within_tolerance": True,
    })
    found = store.lookup("axiom://agree/sha256:f7g8")
    assert found is not None
    assert found["axis_emitted_for"] == "A3"


def test_write_halted_receipt_in_compute_table(store):
    store.write_compute_receipt({
        "uri": "axiom://compute/halt:sha256:7f8a",
        "halted": True,
        "halt_condition": {"name": "lost_particles_rate_exceeds_threshold"},
    })
    found = store.lookup("axiom://compute/halt:sha256:7f8a")
    assert found is not None
    assert found["halted"] is True


def test_lookup_missing_returns_none(store):
    assert store.lookup("axiom://compute/sha256:nonexistent") is None


def test_iter_compute_receipts_returns_all(store):
    for i in range(3):
        store.write_compute_receipt({"uri": f"axiom://compute/sha256:i{i}"})
    receipts = list(store.iter_compute_receipts())
    assert len(receipts) == 3


def test_iter_agreement_receipts_returns_all(store):
    for i in range(4):
        store.write_agreement_receipt({
            "uri": f"axiom://agree/sha256:a{i}",
            "axis_emitted_for": "A3",
        })
    receipts = list(store.iter_agreement_receipts())
    assert len(receipts) == 4


# ----- DuckDB-specific value-add tests -----


def test_query_compute_receipts_with_sql(store):
    """DuckDB enables analytical SQL queries against persisted receipts.

    This is the headline difference vs JSONL: cross-receipt aggregation via
    SQL, not a Python loop.
    """
    for i in range(10):
        store.write_compute_receipt({
            "uri": f"axiom://compute/sha256:r{i}",
            "kernel": "openmc" if i % 2 == 0 else "physcode",
            "value_summary": {"k_eff": 1.0 + 0.001 * i},
        })
    rows = store._conn.execute(
        "SELECT json_extract_string(receipt_json, '$.kernel') AS kernel, COUNT(*) "
        "FROM compute_receipts GROUP BY kernel"
    ).fetchall()
    by_kernel = dict(rows)
    assert by_kernel["openmc"] == 5
    assert by_kernel["physcode"] == 5


def test_persistence_across_store_instances(tmp_path):
    """Closing + reopening the store retains receipts (file-backed durability)."""
    db_path = tmp_path / "bronze.duckdb"
    s1 = DuckDBBronzeReceiptStore(db_path=db_path)
    s1.write_compute_receipt({"uri": "axiom://compute/sha256:persistent"})
    s1.close()

    s2 = DuckDBBronzeReceiptStore(db_path=db_path)
    found = s2.lookup("axiom://compute/sha256:persistent")
    assert found is not None
    s2.close()
