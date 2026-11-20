# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""ADR-069 G1 — chunk schema carries cognitive_type + fragment_ref.

Headless: asserts the DDL + upsert signature without a live Postgres
(the actual round-trip is integration-verified).
"""

from __future__ import annotations

import inspect

from axiom.rag.store import _SCHEMA_SQL, RAGStore


def test_chunks_ddl_has_adr069_columns():
    # Fresh installs (CREATE TABLE) get the columns...
    assert "cognitive_type" in _SCHEMA_SQL
    assert "fragment_ref" in _SCHEMA_SQL


def test_existing_installs_get_idempotent_alters():
    assert "ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS cognitive_type" in _SCHEMA_SQL
    assert "ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS fragment_ref" in _SCHEMA_SQL


def test_indexes_for_filter_and_eviction():
    assert "idx_chunks_cognitive_type" in _SCHEMA_SQL  # RPE filtering (G4)
    assert "idx_chunks_fragment_ref" in _SCHEMA_SQL     # eviction by source (G6)


def test_upsert_chunks_accepts_cognitive_type_and_fragment_ref():
    sig = inspect.signature(RAGStore.upsert_chunks)
    assert "cognitive_type" in sig.parameters
    assert "fragment_ref" in sig.parameters
    # default None → external-doc ingest leaves them NULL
    assert sig.parameters["cognitive_type"].default is None
    assert sig.parameters["fragment_ref"].default is None
