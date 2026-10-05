# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P2b: the generation-quality log lives in ``rag_generation_log``.

``retrieval_log`` is the per-request retrieval *audit* log (the harness
spec's name; site nodes create one in the same database). The
generation-quality table axiom used to create under that name collided with
it: whichever ``CREATE TABLE IF NOT EXISTS`` ran second silently no-op'd and
that writer's INSERTs failed forever. These tests pin the rename and the
bootstrap migration:

* the bootstrap creates ``rag_generation_log`` (never ``retrieval_log``);
* writes and reads in ``axiom.rag.quality`` target ``rag_generation_log``;
* a legacy ``retrieval_log`` with the generation-quality columns is renamed
  in place, rows and index included;
* a ``retrieval_log`` with the audit-log columns is left untouched and
  ``rag_generation_log`` is created beside it.

The headless half drives the bootstrap through a scripted psycopg-shaped
cursor over a tiny in-memory catalog (the store is Postgres-only: ``%s``
params, ``BIGSERIAL``, ``percentile_cont``, so there is no SQLite path to
use). The live half repeats the migration cases on a real Postgres inside a
scratch schema; it needs ``DATABASE_URL`` and the ``integration`` marker.
"""

from __future__ import annotations

import os
import re
import uuid
from types import SimpleNamespace

import pytest

from axiom.rag import generation as gen_mod
from axiom.rag.generation import GenerationManager
from axiom.rag.quality import compute_generation_quality, log_retrieval

# The pre-P2b DDL, verbatim, so the legacy fixture is exactly what old nodes made.
LEGACY_GENERATION_DDL = """\
CREATE TABLE IF NOT EXISTS retrieval_log (
    id              BIGSERIAL PRIMARY KEY,
    query_hash      TEXT NOT NULL,
    corpus          TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    chunking_tier   TEXT,
    result_count    INTEGER,
    top_similarity  FLOAT,
    user_feedback   SMALLINT,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retrieval_log_corpus_gen
    ON retrieval_log (corpus, generation);
"""

# The site node's per-request audit log (harness-spec shape).
AUDIT_DDL = """\
CREATE TABLE IF NOT EXISTS retrieval_log (
    id              BIGSERIAL PRIMARY KEY,
    principal       TEXT,
    source          TEXT,
    query_text      TEXT,
    mode            TEXT,
    k               INTEGER,
    result_count    INTEGER,
    results         JSONB,
    latency_ms      INTEGER,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

GEN_COLS = [
    "id",
    "query_hash",
    "corpus",
    "generation",
    "chunking_tier",
    "result_count",
    "top_similarity",
    "user_feedback",
    "latency_ms",
    "created_at",
]
AUDIT_COLS = [
    "id",
    "principal",
    "source",
    "query_text",
    "mode",
    "k",
    "result_count",
    "results",
    "latency_ms",
    "note",
    "created_at",
]


# --------------------------------------------------------------------------
# Headless: a psycopg-shaped cursor over an in-memory catalog
# --------------------------------------------------------------------------

_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)", re.I)
_CREATE_INDEX_RE = re.compile(r"CREATE INDEX IF NOT EXISTS (\w+)", re.I)
_RENAME_TABLE_RE = re.compile(r"ALTER TABLE (\w+) RENAME TO (\w+)", re.I)
_RENAME_INDEX_RE = re.compile(r"ALTER INDEX IF EXISTS (\w+) RENAME TO (\w+)", re.I)
_INSERT_RE = re.compile(r"INSERT INTO (\w+)", re.I)


def _columns_from_ddl(stmt: str) -> list[str]:
    try:
        body = stmt[stmt.index("(") + 1 : stmt.rindex(")")]
    except ValueError:
        return []
    return [line.strip().split()[0] for line in body.splitlines() if line.strip()]


class FakeCatalog:
    """Tables (name -> columns), rows (name -> inserted params), index names."""

    def __init__(self, tables=None, rows=None, indexes=()):
        self.tables: dict[str, list[str]] = {k: list(v) for k, v in (tables or {}).items()}
        self.rows: dict[str, list] = {k: list(v) for k, v in (rows or {}).items()}
        self.indexes: set[str] = set(indexes)
        self.executed: list[str] = []

    @property
    def statements(self) -> list[str]:
        """Every executed statement, DDL blocks split apart."""
        out = []
        for sql in self.executed:
            out.extend(s.strip() for s in sql.split(";") if s.strip())
        return out


class FakeCursor:
    def __init__(self, catalog: FakeCatalog):
        self.cat = catalog
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.cat.executed.append(sql)
        self._result = []
        if "information_schema.columns" in sql:
            self._result = [(c,) for c in self.cat.tables.get(params[0], [])]
            return
        for stmt in (s.strip() for s in sql.split(";")):
            if not stmt:
                continue
            if m := _RENAME_TABLE_RE.match(stmt):
                old, new = m.groups()
                if new in self.cat.tables:
                    raise RuntimeError(f'relation "{new}" already exists')
                self.cat.tables[new] = self.cat.tables.pop(old)
                self.cat.rows[new] = self.cat.rows.pop(old, [])
            elif m := _RENAME_INDEX_RE.match(stmt):
                old, new = m.groups()
                if old in self.cat.indexes:
                    self.cat.indexes.remove(old)
                    self.cat.indexes.add(new)
            elif m := _CREATE_TABLE_RE.match(stmt):
                self.cat.tables.setdefault(m.group(1), _columns_from_ddl(stmt))
                self.cat.rows.setdefault(m.group(1), [])
            elif m := _CREATE_INDEX_RE.match(stmt):
                self.cat.indexes.add(m.group(1))
            elif m := _INSERT_RE.match(stmt):
                table = m.group(1)
                if table not in self.cat.tables:
                    raise RuntimeError(f'relation "{table}" does not exist')
                self.cat.rows[table].append(params)
            elif stmt.upper().startswith("SELECT"):
                # quality.py aggregates: count/avg/p50/latency, then (positive, total)
                self._result = [(0, 0, 0, 0)]

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    closed = False

    def __init__(self, catalog: FakeCatalog):
        self.cat = catalog

    def cursor(self, **_kw):
        return FakeCursor(self.cat)


def _store(catalog: FakeCatalog):
    return SimpleNamespace(_conn=FakeConn(catalog))


def _alters(cat: FakeCatalog) -> list[str]:
    return [s for s in cat.statements if s.upper().startswith("ALTER")]


class TestBootstrapDdl:
    def test_ddl_names_rag_generation_log_not_retrieval_log(self):
        ddl = gen_mod._RAG_GENERATION_LOG_DDL
        assert "CREATE TABLE IF NOT EXISTS rag_generation_log" in ddl
        assert "idx_rag_generation_log_corpus_gen" in ddl
        assert "retrieval_log" not in ddl
        assert not hasattr(gen_mod, "_RETRIEVAL_LOG_DDL")

    def test_generation_manager_bootstrap_creates_rag_generation_log(self):
        cat = FakeCatalog()
        GenerationManager(_store(cat))
        assert "rag_generation_log" in cat.tables
        assert "rag_generation_config" in cat.tables
        assert "retrieval_log" not in cat.tables
        assert "idx_rag_generation_log_corpus_gen" in cat.indexes
        assert set(cat.tables["rag_generation_log"]) == set(GEN_COLS)

    def test_bootstrap_is_idempotent_and_never_alters_a_fresh_db(self):
        cat = FakeCatalog()
        GenerationManager(_store(cat))
        GenerationManager(_store(cat))
        assert _alters(cat) == []
        assert set(cat.tables) == {"rag_generation_log", "rag_generation_config"}

    def test_bootstrap_swallows_db_errors(self):
        class Boom:
            _conn = SimpleNamespace(cursor=lambda: (_ for _ in ()).throw(RuntimeError("down")))

        GenerationManager(Boom())  # must not raise

    def test_bootstrap_without_connection_is_a_noop(self):
        gm = GenerationManager(SimpleNamespace(_conn=None))
        assert gm.get_active_generation("rag-community") == 1


class TestQualityReadWrite:
    def test_log_retrieval_inserts_into_rag_generation_log(self):
        cat = FakeCatalog()
        store = _store(cat)
        GenerationManager(store)
        log_retrieval(
            store,
            query_hash="abc123",
            corpus="rag-community",
            generation=1,
            chunking_tier="fixed",
            result_count=5,
            top_similarity=0.85,
            latency_ms=42,
        )
        assert cat.rows["rag_generation_log"] == [
            ("abc123", "rag-community", 1, "fixed", 5, 0.85, None, 42)
        ]
        inserts = [s for s in cat.statements if s.upper().startswith("INSERT")]
        assert len(inserts) == 1 and inserts[0].startswith("INSERT INTO rag_generation_log ")

    def test_log_retrieval_leaves_an_audit_retrieval_log_alone(self):
        cat = FakeCatalog(
            tables={"retrieval_log": AUDIT_COLS}, rows={"retrieval_log": ["site-row"]}
        )
        store = _store(cat)
        GenerationManager(store)
        log_retrieval(store, query_hash="h", corpus="rag-community", generation=1)
        assert cat.rows["retrieval_log"] == ["site-row"]
        assert len(cat.rows["rag_generation_log"]) == 1

    def test_compute_generation_quality_reads_rag_generation_log(self):
        cat = FakeCatalog()
        store = _store(cat)
        GenerationManager(store)
        q = compute_generation_quality(store, "rag-community", generation=1)
        assert q.query_count == 0
        selects = [
            s for s in cat.statements if s.upper().startswith("SELECT") and "COUNT(*)" in s.upper()
        ]
        assert len(selects) == 2
        assert all("FROM rag_generation_log " in s for s in selects)
        assert not any("retrieval_log" in s for s in selects)


class TestLegacyMigration:
    def test_generation_quality_retrieval_log_is_renamed_and_rows_survive(self):
        cat = FakeCatalog(
            tables={"retrieval_log": GEN_COLS},
            rows={"retrieval_log": ["old-1", "old-2"]},
            indexes={"idx_retrieval_log_corpus_gen"},
        )
        GenerationManager(_store(cat))
        assert "retrieval_log" not in cat.tables
        assert cat.tables["rag_generation_log"] == GEN_COLS
        assert cat.rows["rag_generation_log"] == ["old-1", "old-2"]
        assert cat.indexes == {"idx_rag_generation_log_corpus_gen"}
        assert _alters(cat) == [
            "ALTER TABLE retrieval_log RENAME TO rag_generation_log",
            "ALTER INDEX IF EXISTS idx_retrieval_log_corpus_gen "
            "RENAME TO idx_rag_generation_log_corpus_gen",
        ]

    def test_rename_runs_before_create(self):
        cat = FakeCatalog(tables={"retrieval_log": GEN_COLS}, rows={"retrieval_log": ["old"]})
        GenerationManager(_store(cat))
        stmts = cat.statements
        rename_at = next(i for i, s in enumerate(stmts) if s.startswith("ALTER TABLE"))
        create_at = next(
            i
            for i, s in enumerate(stmts)
            if s.startswith("CREATE TABLE IF NOT EXISTS rag_generation_log")
        )
        assert rename_at < create_at

    def test_audit_shaped_retrieval_log_is_untouched_and_new_table_created_beside_it(self):
        cat = FakeCatalog(
            tables={"retrieval_log": AUDIT_COLS}, rows={"retrieval_log": ["site-row"]}
        )
        GenerationManager(_store(cat))
        assert cat.tables["retrieval_log"] == AUDIT_COLS
        assert cat.rows["retrieval_log"] == ["site-row"]
        assert "rag_generation_log" in cat.tables
        assert _alters(cat) == []

    def test_mixed_shape_is_left_alone(self):
        # Has our markers AND an audit marker: ambiguous, so never renamed.
        cat = FakeCatalog(tables={"retrieval_log": GEN_COLS + ["query_text"]})
        GenerationManager(_store(cat))
        assert "retrieval_log" in cat.tables
        assert "rag_generation_log" in cat.tables
        assert _alters(cat) == []

    def test_unknown_shape_is_left_alone(self):
        cat = FakeCatalog(tables={"retrieval_log": ["id", "note"]})
        GenerationManager(_store(cat))
        assert "retrieval_log" in cat.tables
        assert "rag_generation_log" in cat.tables
        assert _alters(cat) == []

    def test_existing_rag_generation_log_is_never_clobbered(self):
        cat = FakeCatalog(
            tables={"retrieval_log": GEN_COLS, "rag_generation_log": GEN_COLS},
            rows={"retrieval_log": ["legacy"], "rag_generation_log": ["new"]},
        )
        GenerationManager(_store(cat))
        assert cat.rows["retrieval_log"] == ["legacy"]
        assert cat.rows["rag_generation_log"] == ["new"]
        assert _alters(cat) == []

    def test_nothing_is_ever_dropped(self):
        for tables in ({"retrieval_log": GEN_COLS}, {"retrieval_log": AUDIT_COLS}, {}):
            cat = FakeCatalog(tables=tables)
            GenerationManager(_store(cat))
            assert not any("DROP" in s.upper() for s in cat.statements)

    def test_migrate_returns_whether_it_renamed(self):
        cat = FakeCatalog(tables={"retrieval_log": GEN_COLS})
        cur = FakeCursor(cat)
        assert gen_mod.migrate_legacy_generation_log(cur) is True
        assert gen_mod.migrate_legacy_generation_log(cur) is False

    def test_store_connect_runs_the_migration(self, monkeypatch):
        """RAGStore.connect() bootstraps through the same path as GenerationManager."""
        import psycopg2

        from axiom.rag.store import RAGStore

        cat = FakeCatalog(tables={"retrieval_log": GEN_COLS}, rows={"retrieval_log": ["old"]})
        conn = FakeConn(cat)
        conn.autocommit = False
        monkeypatch.setattr(psycopg2, "connect", lambda dsn: conn)
        RAGStore("postgresql://fake/db").connect()
        assert "retrieval_log" not in cat.tables
        assert cat.rows["rag_generation_log"] == ["old"]
        assert "ALTER TABLE retrieval_log RENAME TO rag_generation_log" in cat.statements


# --------------------------------------------------------------------------
# Live Postgres: the same cases in a throwaway schema
# --------------------------------------------------------------------------

_LIVE = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — requires live PG",
)


@pytest.fixture
def scratch_conn():
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    schema = f"p2b_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"SET search_path TO {schema}")
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        conn.close()


def _regclass(cur, name: str):
    cur.execute("SELECT to_regclass(%s)", (name,))
    return cur.fetchone()[0]


def _indexes(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = %s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


@pytest.mark.integration
@_LIVE
class TestLegacyMigrationLive:
    def test_generation_quality_retrieval_log_is_renamed_and_rows_survive(self, scratch_conn):
        with scratch_conn.cursor() as cur:
            cur.execute(LEGACY_GENERATION_DDL)
            cur.execute(
                "INSERT INTO retrieval_log (query_hash, corpus, generation, top_similarity, latency_ms) "
                "VALUES ('h1', 'rag-community', 1, 0.9, 10), ('h2', 'rag-community', 1, 0.7, 30)"
            )
        store = SimpleNamespace(_conn=scratch_conn)
        GenerationManager(store)

        with scratch_conn.cursor() as cur:
            assert _regclass(cur, "retrieval_log") is None
            assert _regclass(cur, "rag_generation_log") is not None
            cur.execute("SELECT query_hash FROM rag_generation_log ORDER BY id")
            assert [r[0] for r in cur.fetchall()] == ["h1", "h2"]
            idx = _indexes(cur, "rag_generation_log")
            assert "idx_rag_generation_log_corpus_gen" in idx
            assert "idx_retrieval_log_corpus_gen" not in idx

        # A second bootstrap is a no-op, and the quality write/read path works.
        GenerationManager(store)
        log_retrieval(
            store, query_hash="h3", corpus="rag-community", generation=1, top_similarity=0.8
        )
        q = compute_generation_quality(store, "rag-community", generation=1)
        assert q.query_count == 3
        assert q.mean_similarity == pytest.approx(0.8)

    def test_audit_shaped_retrieval_log_is_untouched(self, scratch_conn):
        with scratch_conn.cursor() as cur:
            cur.execute(AUDIT_DDL)
            cur.execute(
                "INSERT INTO retrieval_log (principal, query_text, mode, k) "
                "VALUES ('@ben:site', 'what is the rod worth', 'hybrid', 5)"
            )
        store = SimpleNamespace(_conn=scratch_conn)
        GenerationManager(store)
        log_retrieval(store, query_hash="h1", corpus="rag-community", generation=1)

        with scratch_conn.cursor() as cur:
            assert _regclass(cur, "retrieval_log") is not None
            assert _regclass(cur, "rag_generation_log") is not None
            cur.execute("SELECT principal, query_text FROM retrieval_log")
            assert cur.fetchall() == [("@ben:site", "what is the rod worth")]
            cur.execute("SELECT count(*) FROM rag_generation_log")
            assert cur.fetchone()[0] == 1
