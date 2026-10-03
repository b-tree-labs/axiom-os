# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The store must work against either embedding column type.

Measured on a live corpus: float16 `halfvec` holds recall@10 at 1.0000 for half
the bytes, while truncating to 384 dimensions — the same 1536 bytes — holds
only 0.7933. So halfvec is worth migrating to, and a migration means the store
will meet BOTH column types in the field: installs that have converted and
installs that have not.

pgvector has no `halfvec <=> vector` operator, so a hardcoded `::vector` cast
does not merely lose precision against a converted column — it fails to resolve
and every vector search errors. The cast has to follow the column.
"""

from __future__ import annotations

import pytest

from axiom.rag.store import (
    SUPPORTED_VECTOR_TYPES,
    detect_vector_type,
    existing_vector_type,
    opclass_for,
    schema_sql,
)


class _Cur:
    """Minimal cursor double: returns one row for the type probe."""

    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return self._row


def test_detects_a_converted_halfvec_column():
    assert detect_vector_type(_Cur(("halfvec",))) == "halfvec"


def test_detects_an_unconverted_vector_column():
    assert detect_vector_type(_Cur(("vector",))) == "vector"


def test_a_missing_table_falls_back_to_vector_not_an_error():
    """Schema-ensure runs before the table exists on a fresh install. Raising
    there would make a first connect fail."""
    assert detect_vector_type(_Cur(None)) == "vector"


def test_an_unrecognised_type_falls_back_to_vector():
    """A column type nobody planned for must not silently produce SQL that
    cannot parse. Fall back to the type the schema declares."""
    assert detect_vector_type(_Cur(("bytea",))) == "vector"


def test_a_probe_that_raises_does_not_break_connect():
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("relation does not exist")

        def fetchone(self):
            raise AssertionError("should not be reached")

    assert detect_vector_type(_Boom()) == "vector"


@pytest.mark.parametrize("vt,expected", [("vector", "vector_cosine_ops"),
                                         ("halfvec", "halfvec_cosine_ops")])
def test_the_index_opclass_follows_the_column_type(vt, expected):
    """An ivfflat index built with the wrong operator class does not exist as
    far as the planner is concerned, so the query silently seq-scans 2 million
    rows instead of erroring."""
    assert opclass_for(vt) == expected


def test_schema_sql_declares_the_requested_type_in_both_places():
    """The column declaration and the index opclass must agree, or CREATE INDEX
    fails on a fresh install."""
    sql = schema_sql("halfvec")
    assert "embedding       halfvec(768)" in sql
    assert "halfvec_cosine_ops" in sql
    assert "vector_cosine_ops" not in sql


def test_schema_sql_default_is_unchanged_for_existing_installs():
    """Changing the default would silently convert every new install to a type
    their tooling may not expect. halfvec is opt-in until that is a decision
    somebody makes deliberately."""
    sql = schema_sql()
    assert "embedding       vector(768)" in sql
    assert "vector_cosine_ops" in sql


def test_only_the_two_known_types_are_accepted():
    assert SUPPORTED_VECTOR_TYPES == ("vector", "halfvec")
    with pytest.raises(ValueError):
        schema_sql("float8[]")


# --- the substitution actually reaches the query ------------------------------


def test_search_sql_carries_no_unsubstituted_placeholder():
    """A `{vec}` that survives into the database is a syntax error at query
    time, which is the worst place to find it: only on a real search, only
    against a real corpus."""
    import inspect

    from axiom.rag.store import RAGStore

    src = inspect.getsource(RAGStore.search)
    assert "{vec}" in src, "the placeholder should still be in the SQL"
    assert 'sql.replace("{vec}", self._vector_type)' in src, (
        "search() must substitute the placeholder before executing"
    )


def test_store_defaults_to_vector_before_it_connects():
    """An unconnected store must not claim a type it has not probed."""
    from axiom.rag.store import RAGStore

    store = RAGStore("postgresql://nobody@localhost/none", ensure_schema=False)
    assert store._vector_type == "vector"


# --- the database-level probes default ----------------------------------------
#
# Measured on the live corpus: at pgvector's session default of probes=1 the
# index returned 0.5700 recall@10 against exact; at 72 it returned 0.9167. The
# store sets probes per connection, so the platform's own path was always fine
# — but psql, a notebook, a sibling service or a future extension querying the
# same table got 57% of the right answers with no error and plausible output.
#
# A default nobody has to know about is worth more than a helper only the
# platform calls.


def test_probes_default_sql_uses_the_recommended_value():
    from axiom.rag.store import probes_default_sql, recommended_probes

    sql = probes_default_sql("axiom_db", 2_098_293)
    assert str(recommended_probes(2_098_293)) in sql
    assert "ALTER DATABASE" in sql
    assert '"axiom_db"' in sql


def test_probes_default_quotes_the_database_name():
    """The name comes from a connection string, so it is not a literal this
    code chose. Quoting it keeps a hyphenated or reserved name working, and
    keeps the statement from being something else entirely."""
    from axiom.rag.store import probes_default_sql

    assert '"my-rag-db"' in probes_default_sql("my-rag-db", 1000)


def test_probes_default_refuses_a_name_it_cannot_safely_quote():
    """A database name containing a double quote cannot be interpolated into
    an identifier safely, and ALTER DATABASE takes no parameters. Refuse rather
    than emit something that might parse as more than one statement."""
    import pytest

    from axiom.rag.store import probes_default_sql

    with pytest.raises(ValueError):
        pytest.importorskip("axiom.rag.store")
        probes_default_sql('evil"; DROP DATABASE axiom_db; --', 1000)


def test_an_empty_corpus_still_gets_a_usable_floor():
    """A fresh install has no rows, and a default of 1 probe there would be the
    very cliff this exists to prevent once it fills up."""
    from axiom.rag.store import probes_default_sql, recommended_probes

    assert recommended_probes(0) >= 10
    assert str(recommended_probes(0)) in probes_default_sql("axiom_db", 0)


def test_ensure_probes_default_never_raises_on_a_restricted_role():
    """ALTER DATABASE needs ownership. A deployment connecting as a restricted
    role is a legitimate configuration, not a failure — it keeps the old
    default and the per-connection setting still applies."""
    from axiom.rag.store import RAGStore

    class _Denied:
        def execute(self, *a, **k):
            raise RuntimeError("must be owner of database axiom_db")

        def fetchone(self):
            raise AssertionError("unreachable")

    store = RAGStore("postgresql://nobody@localhost/none", ensure_schema=False)
    store._ensure_probes_default(_Denied())  # must not raise


def test_ensure_probes_default_does_not_write_when_already_correct():
    """A reconnect must not be a write. Schema-ensure runs on every connect."""
    from axiom.rag.store import RAGStore, recommended_probes

    want = recommended_probes(2_098_293)

    class _Cur:
        def __init__(self):
            self.statements = []
            self._answers = [("axiom_db",), (2_098_293,), (str(want),)]

        def execute(self, sql, params=None):
            self.statements.append(sql)

        def fetchone(self):
            return self._answers.pop(0)

    cur = _Cur()
    RAGStore("postgresql://x@localhost/n", ensure_schema=False)._ensure_probes_default(cur)
    assert not any("ALTER DATABASE" in s for s in cur.statements)


def test_create_store_accepts_and_honours_ensure_schema():
    """Tested against the REAL factory, not a double.

    A double that took **kw once accepted a keyword the real function did not
    have: the unit test passed and the node raised TypeError at the first call.
    A stub more permissive than the thing it stands for proves nothing.
    """
    import inspect

    from axiom.rag.store_factory import create_store

    sig = inspect.signature(create_store)
    assert "ensure_schema" in sig.parameters
    assert sig.parameters["ensure_schema"].default is True

    store = create_store("postgresql://nobody@localhost/none", ensure_schema=False)
    assert store._ensure_schema is False


# -- ensure follows the column that is already there --------------------------


def test_existing_halfvec_column_is_reported():
    assert existing_vector_type(_Cur(("halfvec",))) == "halfvec"


def test_no_table_reports_none_so_the_default_applies():
    assert existing_vector_type(_Cur(None)) is None


def test_unknown_type_reports_none():
    assert existing_vector_type(_Cur(("sparsevec",))) is None


def test_probe_that_raises_reports_none():
    class _Boom(_Cur):
        def execute(self, sql, params=None):
            raise RuntimeError("permission denied")

    assert existing_vector_type(_Boom(None)) is None


def test_ensure_ddl_uses_the_existing_columns_opclass():
    """The nightly-refresh crash: chunks.embedding is halfvec, the ensure DDL
    named vector_cosine_ops, and Postgres resolves the opclass before it
    honours IF NOT EXISTS."""
    ddl = schema_sql(existing_vector_type(_Cur(("halfvec",))))
    assert "halfvec_cosine_ops" in ddl
    assert "vector_cosine_ops" not in ddl.replace("halfvec_cosine_ops", "")


def test_fresh_install_ddl_keeps_the_default():
    ddl = schema_sql(existing_vector_type(_Cur(None)))
    assert "embedding       vector(768)" in ddl
