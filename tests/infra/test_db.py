# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.infra.db` — the schema-per-extension primitive (ADR-052).

Pure-logic tests run anywhere; integration tests (schema isolation) skip when
Postgres isn't reachable so the suite stays green on dev boxes without
`axi db up` running.
"""

from __future__ import annotations

import os

import pytest

from axiom.infra.db import normalize_extension_name

# ---------------------------------------------------------------------------
# Pure logic — runs anywhere
# ---------------------------------------------------------------------------


class TestNormalizeExtensionName:
    @pytest.fixture(autouse=True)
    def _no_worker_suffix(self, monkeypatch):
        # These assert the production normalization contract (no suffix). The
        # xdist conftest sets AXIOM_TEST_SCHEMA_SUFFIX per worker, which would
        # otherwise leak in (e.g. "model_corral_gw0"); clear it here.
        monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)

    def test_simple(self):
        assert normalize_extension_name("expman") == "expman"

    def test_hyphens_become_underscores(self):
        assert normalize_extension_name("model-corral") == "model_corral"

    def test_lowercased(self):
        assert normalize_extension_name("ExpMan") == "expman"

    def test_unsafe_chars_replaced(self):
        assert normalize_extension_name("ext$weird!") == "ext_weird_"

    def test_length_capped_at_postgres_max(self):
        assert len(normalize_extension_name("a" * 100)) == 63

    def test_leading_unsafe_stripped(self):
        assert normalize_extension_name("---weird") == "weird"

    def test_all_unsafe_yields_safe_fallback(self):
        # Must never return an invalid identifier; fall back to "ext".
        assert normalize_extension_name("---") == "ext"

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_empty_raises(self, bad):
        with pytest.raises(ValueError):
            normalize_extension_name(bad)


# ---------------------------------------------------------------------------
# Integration — needs Postgres reachable at AXIOM_DB_URL
# ---------------------------------------------------------------------------


def _pg_available() -> bool:
    try:
        import psycopg2  # type: ignore

        url = os.environ.get(
            "AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db"
        )
        conn = psycopg2.connect(url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pg_only = pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable")


@pg_only
class TestSchemaPerExtension:
    def test_ensure_schema_idempotent(self):
        from axiom.infra.db import ensure_schema, get_engine

        engine = get_engine()
        s1 = ensure_schema(engine, "test_db_a")
        s2 = ensure_schema(engine, "test_db_a")
        assert s1 == s2 == "test_db_a"

    def test_session_search_path_scoped_to_extension(self):
        from sqlalchemy import text

        from axiom.infra.db import session_for

        with session_for("test_db_a") as s:
            sp = s.execute(text("SHOW search_path")).scalar()
            assert "test_db_a" in sp

    def test_two_extensions_cannot_see_each_others_tables(self):
        # The whole point of schema-per-extension: extension B never sees
        # extension A's tables via unqualified names.
        from sqlalchemy import text

        from axiom.infra.db import session_for

        try:
            with session_for("test_db_a") as a:
                a.execute(text("CREATE TABLE IF NOT EXISTS only_in_a (id int)"))
                a.commit()

            with session_for("test_db_b") as b:
                seen = b.execute(
                    text("SELECT to_regclass('only_in_a') IS NOT NULL")
                ).scalar()
                assert seen is False, "extension B should not see A's tables"
        finally:
            from sqlalchemy import text as t

            from axiom.infra.db import session_for as sf

            with sf("test_db_a") as a:
                a.execute(t("DROP TABLE IF EXISTS only_in_a"))
                a.commit()

    def test_engine_and_pool_are_shared(self):
        from axiom.infra.db import get_engine

        assert get_engine() is get_engine()

    def test_engine_for_returns_shared_engine_and_schema(self):
        from axiom.infra.db import engine_for, get_engine

        engine, schema = engine_for("test_db_a")
        assert engine is get_engine()
        assert schema == "test_db_a"


# ---------------------------------------------------------------------------
# Test-only schema suffix — isolates persisted-state tests under xdist so
# parallel workers don't TRUNCATE each other's rows. Honored ONLY when the
# env var is set; production behavior is untouched.
# ---------------------------------------------------------------------------


class TestSchemaSuffixHook:
    """``AXIOM_TEST_SCHEMA_SUFFIX``-aware schema resolution.

    The conftest sets this per xdist worker so ``session_for("vault")``
    on worker gw0 resolves to schema ``vault_gw0``, on gw1 to
    ``vault_gw1``, etc. Without xdist (or with the var unset),
    behavior is unchanged.
    """

    def test_unset_returns_bare_name(self, monkeypatch):
        from axiom.infra.db import normalize_extension_name

        monkeypatch.delenv("AXIOM_TEST_SCHEMA_SUFFIX", raising=False)
        assert normalize_extension_name("vault") == "vault"

    def test_set_appends_suffix(self, monkeypatch):
        from axiom.infra.db import normalize_extension_name

        monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", "_gw3")
        assert normalize_extension_name("vault") == "vault_gw3"

    def test_suffix_is_normalized_too(self, monkeypatch):
        """Suffix runs through the same normalizer so a malformed
        worker id ('gw-3') becomes a valid schema ident ('gw_3')."""
        from axiom.infra.db import normalize_extension_name

        monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", "_gw-3")
        # Hyphens become underscores per the existing normalizer.
        assert normalize_extension_name("vault") == "vault_gw_3"

    def test_suffix_capped_at_postgres_max(self, monkeypatch):
        """Combined name still respects Postgres 63-byte identifier limit."""
        from axiom.infra.db import normalize_extension_name

        monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", "_" + "x" * 100)
        assert len(normalize_extension_name("vault")) <= 63

    @pg_only
    def test_session_for_picks_up_suffix(self, monkeypatch):
        from sqlalchemy import text

        from axiom.infra.db import session_for

        monkeypatch.setenv("AXIOM_TEST_SCHEMA_SUFFIX", "_gw9")
        with session_for("vault") as s:
            sp = s.execute(text("SHOW search_path")).scalar()
            assert "vault_gw9" in sp, sp
