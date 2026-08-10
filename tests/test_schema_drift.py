# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests that prevent schema DDL drift.

The RAG store's _SCHEMA_SQL must include ALL columns that any code path
references. When this test fails, it means someone added a column reference
in Python code without updating the CREATE TABLE DDL — which will break
fresh installs (like CI).
"""

from __future__ import annotations

import re


class TestSchemaCompleteness:
    """Ensure DDL and code reference the same columns."""

    def test_documents_ddl_has_all_referenced_columns(self):
        """Every column referenced in store.py queries must exist in the DDL."""
        from axiom.rag.store import _SCHEMA_SQL

        # Extract column names from CREATE TABLE documents DDL
        doc_match = re.search(
            r"CREATE TABLE.*?documents\s*\((.*?)\);",
            _SCHEMA_SQL, re.DOTALL | re.IGNORECASE,
        )
        assert doc_match, "Could not find documents CREATE TABLE in _SCHEMA_SQL"
        ddl_text = doc_match.group(1)
        ddl_columns = {
            line.strip().split()[0].lower()
            for line in ddl_text.splitlines()
            if line.strip() and not line.strip().startswith(("UNIQUE", "PRIMARY", "--", ")"))
        }

        # Columns referenced in get_document and find_by_content_hash SELECTs
        required = {
            "source_path", "corpus", "checksum", "content_hash",
            "chunk_count", "last_indexed", "id", "source_type", "title",
            "owner", "data_source", "sync_id", "first_indexed",
        }

        missing = required - ddl_columns
        assert not missing, (
            f"Columns referenced in code but missing from documents DDL: {missing}. "
            "Update _SCHEMA_SQL in store.py to include these columns."
        )

    def test_chunks_ddl_has_all_referenced_columns(self):
        """Every column referenced in store.py queries must exist in the DDL."""
        from axiom.rag.store import _SCHEMA_SQL

        chunk_match = re.search(
            r"CREATE TABLE.*?chunks\s*\((.*?)\);",
            _SCHEMA_SQL, re.DOTALL | re.IGNORECASE,
        )
        assert chunk_match, "Could not find chunks CREATE TABLE in _SCHEMA_SQL"
        ddl_text = chunk_match.group(1)
        ddl_columns = {
            line.strip().split()[0].lower()
            for line in ddl_text.splitlines()
            if line.strip() and not line.strip().startswith(("UNIQUE", "PRIMARY", "--", ")"))
        }

        required = {
            "id", "source_path", "source_title", "source_type",
            "chunk_text", "chunk_index", "start_line", "embedding",
            "corpus", "owner", "team", "checksum", "chunking_tier",
            "indexed_at", "updated_at",
        }

        missing = required - ddl_columns
        assert not missing, (
            f"Columns referenced in code but missing from chunks DDL: {missing}. "
            "Update _SCHEMA_SQL in store.py to include these columns."
        )

    def test_embedding_dimension_is_768(self):
        """Schema must use 768-dim vectors (nomic-embed-text)."""
        from axiom.rag.store import _SCHEMA_SQL

        assert "vector(768)" in _SCHEMA_SQL, (
            "Schema DDL does not use vector(768). "
            "Embedding dimension must match nomic-embed-text (768)."
        )
        assert "vector(1536)" not in _SCHEMA_SQL, (
            "Schema DDL still has vector(1536) — must be 768 for nomic-embed-text."
        )

    def test_sqlite_store_has_same_columns(self):
        """SQLite store schema must define the same columns as PG store."""
        from axiom.rag.sqlite_store import _SCHEMA_SQL as sqlite_schema
        from axiom.rag.store import _SCHEMA_SQL as pg_schema

        # Extract column names from both
        def extract_columns(schema, table):
            match = re.search(
                rf"CREATE TABLE.*?{table}\s*\((.*?)\);",
                schema, re.DOTALL | re.IGNORECASE,
            )
            if not match:
                return set()
            return {
                line.strip().split()[0].lower()
                for line in match.group(1).splitlines()
                if line.strip() and not line.strip().startswith(("UNIQUE", "PRIMARY", "--", ")"))
            }

        for table in ("documents", "chunks"):
            pg_cols = extract_columns(pg_schema, table)
            sq_cols = extract_columns(sqlite_schema, table)
            # SQLite won't have 'embedding' column (stored in vec0 table)
            sq_cols.discard("embedding")  # OK — sqlite uses vec0 virtual table
            pg_only = pg_cols - sq_cols - {"embedding"}
            sq_only = sq_cols - pg_cols
            assert not pg_only, (
                f"PG {table} has columns missing from SQLite: {pg_only}"
            )
            assert not sq_only, (
                f"SQLite {table} has extra columns not in PG: {sq_only}"
            )
