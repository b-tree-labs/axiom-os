# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL / pgvector storage for RAG chunks.

Three-tier corpus model:
  rag-community  — pre-indexed community knowledge (ships with pip package)
  rag-org        — facility/organization corpus (admin-managed)
  rag-internal   — personal workspace index (built during install + post-push)

Uses ``psycopg2`` for database access.  The schema is created automatically
on first ``connect()`` call if it does not exist.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

from .chunker import Chunk
from .fts_query import (
    FTS_CODE_RE,
    FTS_STOP,
    fts_entity_terms,
    fts_entity_tsquery,
    fts_terms,
    fts_tsquery,
)
from .titles import clean_title

log = logging.getLogger(__name__)

CORPUS_COMMUNITY = "rag-community"
CORPUS_ORG = "rag-org"
CORPUS_INTERNAL = "rag-internal"

ALL_CORPORA = (CORPUS_COMMUNITY, CORPUS_ORG, CORPUS_INTERNAL)

#: The access tiers a chunk may be stored with. The first four are the retrieval filter's order
#: (``retriever._TIER_ORDER``). ``export_controlled`` is the routing tier of controlled content: it is stored
#: faithfully, and the filter has no reader for it yet, so it is hidden from every reader (fail closed).
ACCESS_TIERS = ("public", "course", "institutional", "classified", "export_controlled")


#: The labels a document has before anything declares one.
DEFAULT_LABELS = ("public", "unclassified", None)


def check_access_labels(
    access_tier: str, classification: str, allowed_nationalities: tuple[str, ...] | list[str] | None
) -> list[str] | None:
    """Refuse labels the retrieval filter cannot read; return the nationalities as a list (or ``None``)."""
    if access_tier not in ACCESS_TIERS:
        raise ValueError(f"access_tier must be one of {', '.join(ACCESS_TIERS)}; got {access_tier!r}")
    if not isinstance(classification, str) or not classification.strip():
        raise ValueError("classification must be a non-empty string")
    if allowed_nationalities is None:
        return None
    nats = [str(n).strip() for n in allowed_nationalities if str(n).strip()]
    if not nats:
        raise ValueError("allowed_nationalities must name at least one nationality, or be None (unrestricted)")
    return nats


class SchemaChangeRefused(PermissionError):
    """The store's role may not change its schema (#2100). ``str()`` is one sentence with the fix."""


def _schema_refused(exc: Exception) -> SchemaChangeRefused:
    reason = (str(exc).strip().splitlines() or ["permission denied"])[0]
    return SchemaChangeRefused(
        f"The RAG store refused a schema change ({reason}): writes and schema upgrades need the "
        "role that owns the tables, so set rag.write_database_url to that role's postgresql:// URL "
        "(or export DATABASE_URL for one run)."
    )


def expected_columns() -> dict[str, list[str]]:
    """The columns this code's schema declares for ``documents`` and ``chunks``, in order."""
    out: dict[str, list[str]] = {}
    for table in ("documents", "chunks"):
        m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", _SCHEMA_SQL, re.S)
        cols: list[str] = []
        for line in (m.group(1) if m else "").splitlines():
            word = line.strip().split(" ", 1)[0].rstrip(",")
            if word and not word.startswith("--") and word.upper() not in ("UNIQUE", "PRIMARY", "CONSTRAINT"):
                cols.append(word)
        out[table] = cols
    for table, col in re.findall(r"ALTER TABLE (\w+)\s+ADD COLUMN IF NOT EXISTS (\w+)", _SCHEMA_SQL):
        if col not in out.setdefault(table, []):
            out[table].append(col)
    return out


class EmbeddingModelMismatch(ValueError):
    """Vectors from a second embedding model offered to a corpus generation that declared another.

    Two models' vectors live in different spaces, so mixing them in one corpus ranks one model's
    documents by noise for a query embedded by the other, with no error anywhere. Refused before
    anything is written.
    """


def mismatch_message(corpus: str, declared: str, offered: str, source_path: str, generation: int) -> str:
    return (
        f"corpus {corpus!r} (generation {generation}) declared {declared!r}; refusing vectors from "
        f"{offered!r} for {source_path!r}. Embed it with {declared!r}, or build a new generation "
        "(or use another corpus) for the new model."
    )


#: One row per corpus generation: the model its vectors come from, declared by the first write
#: that carried vectors and a model name (``axiom.rag.generation`` creates it).
EMBEDDING_MODEL_TABLE = "rag_corpus_embedding_model"


#: Corpora holding one tenant's own material are named by this prefix plus
#: the tenant id. They are the retrieval isolation axis: a served surface
#: names the corpora it may read, and naming one tenant's corpus is what
#: makes the read that tenant's rather than everyone's.
CORPUS_TENANT_PREFIX = "rag-tenant:"

#: A tenant id arrives from a request field, so it is untrusted input that
#: ends up inside a corpus name. Anything that could forge another tenant's
#: name, or split one tenant across two corpora, is refused rather than
#: sanitized: a silently rewritten id would isolate the wrong thing while
#: looking like it worked. Lowercase because two casings of one id are two
#: corpora, and the separator is excluded because it is the thing a forged
#: name would need.
_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def tenant_corpus(tenant_id: str) -> str:
    """The corpus holding ``tenant_id``'s own material.

    Raises:
        ValueError: if ``tenant_id`` is not a well-formed tenant id. The
            refusal is deliberate — see ``_TENANT_ID_RE``.
    """
    if not isinstance(tenant_id, str):
        raise ValueError(f"tenant id must be a string, got {type(tenant_id).__name__}")
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError(
            f"{tenant_id!r} is not a usable tenant id: it must be 1-64 characters "
            "of lowercase letters, digits, dot, underscore or hyphen, starting "
            "with a letter or digit. It is refused rather than cleaned up because "
            "a rewritten id would scope retrieval to a corpus nobody named."
        )
    return f"{CORPUS_TENANT_PREFIX}{tenant_id}"


def is_tenant_corpus(corpus: str) -> bool:
    """Whether ``corpus`` holds one tenant's material rather than a fixed tier."""
    return isinstance(corpus, str) and corpus.startswith(CORPUS_TENANT_PREFIX)


def tenant_of(corpus: str) -> str | None:
    """The tenant ``corpus`` belongs to, or ``None`` for a fixed corpus."""
    if not is_tenant_corpus(corpus):
        return None
    return corpus[len(CORPUS_TENANT_PREFIX) :]


# ---------------------------------------------------------------------------
# Full-text query construction
# ---------------------------------------------------------------------------

# Cap on rows the pure-text FTS branch ranks. An OR-of-terms query can match a
# large fraction of the corpus (measured: 435k, a fifth of the corpus), and
# ORDER BY ts_rank ranks every match -> a slow hang. Bounding the scan keeps a
# broad query fast; precise queries match fewer rows and stay exact.
#
# 2000 rather than 4000, chosen by measurement once the scan was pinned
# deterministic (see search()). Cost is sharply non-linear because the scan now
# always starts at page 0: 500 -> 0.40s, 1000 -> 0.64s, 2000 -> 0.86s,
# 4000 -> 4.06s. Halving the cap buys determinism for LESS latency than the old
# non-deterministic 4000 (0.91-1.20s), so this is not a speed/correctness
# trade — the previous value was simply past a cliff.
TEXT_SCAN_CAP = 2000

# The tsquery builders live in the driver-free ``axiom.rag.fts_query`` so the
# shared retrieval module (``axiom.rag.hybrid``) recalls the same text
# candidates as this store without importing psycopg2. The private names stay
# bound here for existing callers and tests.
_FTS_STOP = FTS_STOP
_FTS_CODE = FTS_CODE_RE
_fts_terms = fts_terms
_fts_tsquery = fts_tsquery
_fts_entity_terms = fts_entity_terms
_fts_entity_tsquery = fts_entity_tsquery


# ---------------------------------------------------------------------------
# Schema DDL — idempotent (IF NOT EXISTS everywhere)
# ---------------------------------------------------------------------------

# Cluster-wide advisory lock serializing the ensure-schema DDL. IF NOT EXISTS
# makes each statement idempotent but NOT concurrent-safe: every ALTER/CREATE
# still takes AccessExclusiveLock, and two connections running the script
# interleave lock acquisition on `chunks` vs `documents` and deadlock
# (observed on main 2026-09-21 under xdist; the same race exists on a node
# where the serving face, orchestrator, and CLI connect concurrently).
# Any fixed 64-bit value works; this one spells "axi:rag!" so collisions with
# other subsystems' advisory keys are implausible and greppable.
SCHEMA_ADVISORY_LOCK_KEY = int.from_bytes(b"axi:rag!", "big", signed=True)

_SCHEMA_SQL = """\
-- Extensions must be created by superuser (done by setup-<host>.sh)
-- CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id              BIGSERIAL PRIMARY KEY,
    source_path     TEXT NOT NULL,
    corpus          TEXT NOT NULL DEFAULT 'rag-internal',
    source_type     TEXT NOT NULL DEFAULT 'markdown',
    title           TEXT NOT NULL DEFAULT '',
    checksum        TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL DEFAULT '',
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    owner           TEXT,
    data_source     TEXT NOT NULL DEFAULT 'local',
    sync_id         TEXT NOT NULL DEFAULT '',
    corpus_generation INTEGER NOT NULL DEFAULT 1,
    graph_extracted_at TIMESTAMPTZ,
    first_indexed   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_indexed    TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_tier          TEXT NOT NULL DEFAULT 'public',
    classification       TEXT NOT NULL DEFAULT 'unclassified',
    allowed_nationalities TEXT[],  -- NULL = unrestricted
    source_url      TEXT,          -- ADR-091: origin system's shareable link (NULL for URL-less kinds)
    source_ref_id   TEXT,          -- ADR-091: origin system's stable id (e.g. Box file id)
    parser          JSONB,         -- #1946: the extractor(s) and versions that produced the text
    UNIQUE (source_path, corpus)
);

CREATE INDEX IF NOT EXISTS idx_documents_content_hash
    ON documents (content_hash) WHERE content_hash != '';

CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL PRIMARY KEY,
    source_path     TEXT NOT NULL,
    source_title    TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT 'markdown',
    chunk_text      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    start_line      INTEGER NOT NULL DEFAULT 1,
    embedding       vector(768),
    corpus          TEXT NOT NULL DEFAULT 'rag-internal',
    owner           TEXT,
    team            TEXT,
    checksum        TEXT NOT NULL DEFAULT '',
    chunking_tier   TEXT NOT NULL DEFAULT 'fixed',
    corpus_generation INTEGER NOT NULL DEFAULT 1,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_tier          TEXT NOT NULL DEFAULT 'public',
    classification       TEXT NOT NULL DEFAULT 'unclassified',
    cognitive_type        TEXT,  -- ADR-069: MIRIX type of a projected fragment (NULL for ingested docs)
    fragment_ref          TEXT,  -- ADR-069: source MemoryFragment id (NULL for ingested docs)
    allowed_nationalities TEXT[]  -- NULL = unrestricted
);

-- Idempotent upgrade for installs that pre-date these columns.
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS access_tier          TEXT NOT NULL DEFAULT 'public';
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS classification       TEXT NOT NULL DEFAULT 'unclassified';
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS allowed_nationalities TEXT[];
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS cognitive_type        TEXT;
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS fragment_ref          TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS access_tier          TEXT NOT NULL DEFAULT 'public';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS classification       TEXT NOT NULL DEFAULT 'unclassified';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS allowed_nationalities TEXT[];
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_url            TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_ref_id         TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS parser                JSONB;

CREATE INDEX IF NOT EXISTS idx_chunks_access_tier    ON chunks (access_tier);
CREATE INDEX IF NOT EXISTS idx_chunks_classification ON chunks (classification);
CREATE INDEX IF NOT EXISTS idx_chunks_cognitive_type ON chunks (cognitive_type);
CREATE INDEX IF NOT EXISTS idx_chunks_fragment_ref   ON chunks (fragment_ref);

CREATE INDEX IF NOT EXISTS idx_chunks_source_path ON chunks (source_path);
CREATE INDEX IF NOT EXISTS idx_chunks_corpus ON chunks (corpus);
CREATE INDEX IF NOT EXISTS idx_chunks_generation ON chunks (corpus, corpus_generation);
CREATE INDEX IF NOT EXISTS idx_chunks_chunking_tier ON chunks (chunking_tier);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
    -- lists=100 is correct HERE because schema-ensure runs against an empty
    -- table. It stops being correct the moment the corpus grows: see
    -- recommended_lists() below, and check_ivfflat_lists() which reports the
    -- drift on the hygiene heartbeat. Nothing rebuilds it automatically —
    -- a rebuild is a maintenance decision, not a side effect of connecting.
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Full-text search: a GIN index on the tsvector expression the search query
-- uses, or `@@` seq-scans the whole table.
-- ⚠️ IF NOT EXISTS guards the NAME, never the EXPRESSION. An index carrying
-- this same expression under a different name (an older schema version, a
-- hand-rename) does NOT satisfy this statement, so the DDL builds a second
-- copy and rebuilds it on every schema-ensure forever. That is how one live
-- corpus paid 1.79 GB twice, as idx_chunks_fts alongside a legacy
-- idx_chunks_tsv. The fix is to RENAME the survivor to the name below, not to
-- drop one and hope. `check_duplicate_index_expressions` watches for it.
CREATE INDEX IF NOT EXISTS idx_chunks_fts ON chunks
    USING gin (to_tsvector('english', chunk_text));
"""


@dataclass
class SearchResult:
    """A single search hit from the RAG store."""

    source_path: str
    source_title: str
    chunk_text: str
    chunk_index: int
    similarity: float
    combined_score: float
    corpus: str = CORPUS_INTERNAL
    # Access metadata, projected from the chunk row so retrieval can enforce it
    # WITHOUT a second lookup. Defaults are the fail-open-for-back-compat values
    # (public / unclassified / unrestricted); a store that does not select these
    # columns yields exactly the pre-projection behaviour. ``retrieve`` reads
    # these when no explicit ``*_lookup`` override is passed.
    access_tier: str = "public"
    classification: str = "unclassified"
    allowed_nationalities: tuple[str, ...] | None = None


def _has_generation_config(cur, table: str = "rag_generation_config") -> bool:
    """Whether ``rag_generation_config`` (the served-generation pointers), or ``table``, exists here."""
    try:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 - a store that cannot say serves generation 1
        return False
    if not row:
        return False
    return bool(row[0] if not isinstance(row, dict) else next(iter(row.values())))


#: The chunk-scan filter every search branch uses; the generation predicate is added after it.
_CORPUS_SCAN = "WHERE corpus = ANY(%s)"


def generation_predicate(corpus_generation: int | None, *, has_config: bool) -> str:
    """The SQL predicate that keeps a chunk scan to one generation per corpus (#1945).

    ``None`` means "what each corpus serves": its ``active_generation`` in
    ``rag_generation_config``, or 1 when the corpus has no row (or the table does not
    exist). A number names that generation for every corpus, which is how an
    evaluation reads a candidate before it is promoted. The number is cast with
    ``int()`` before it is put into the SQL, so it cannot carry anything else.
    """
    if corpus_generation is not None:
        return f"corpus_generation = {int(corpus_generation)}"
    if not has_config:
        return "corpus_generation = 1"
    return (
        "corpus_generation = COALESCE((SELECT g.active_generation FROM rag_generation_config g "
        "WHERE g.corpus = chunks.corpus), 1)"
    )


def _row_val(row: object, key: str, default: Any) -> Any:
    """Read ``key`` from a DB row (dict-like), returning ``default`` when the
    column is absent or SQL-NULL. Keeps the result-construction hot path from
    crashing if a query variant omits a projected column."""
    try:
        val = row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return default
    return val if val is not None else default


#: The embedding column types this store understands.
#:
#: `halfvec` is float16. Measured on a live 2.1M-chunk corpus, it holds
#: recall@10 at 1.0000 for half the bytes, while Matryoshka truncation to 384
#: dimensions — the *same* 1536 bytes — holds only 0.7933. See
#: `scripts/rag/m5_encoding_recall.py`.
SUPPORTED_VECTOR_TYPES = ("vector", "halfvec")

#: Env override for the type a NEW install declares. Existing installs are
#: detected, never rewritten by connecting.
VECTOR_TYPE_ENV = "AXIOM_RAG_VECTOR_TYPE"


def opclass_for(vector_type: str) -> str:
    """The ivfflat operator class matching an embedding column type.

    Getting this wrong does not raise. An ivfflat index built with the wrong
    operator class simply does not exist as far as the planner is concerned, so
    the query seq-scans the whole table and the only symptom is that retrieval
    became slow.
    """
    return f"{vector_type}_cosine_ops"


def existing_vector_type(cur) -> str | None:
    """The embedding column's type if the table already exists, else ``None``.

    ``connect()`` must ensure the schema WITH this type. On 2026-09-25 the
    production node's ``chunks.embedding`` was ``halfvec`` and ``connect()`` ran
    ``schema_sql()`` (default ``vector``) first: Postgres resolves an index's
    operator class before it honours ``IF NOT EXISTS``, so the ensure failed
    with ``operator class "vector_cosine_ops" does not accept data type
    halfvec`` on every nightly refresh, before a single Box call.

    ``None`` (no table, unknown type, probe raised) lets the caller fall back
    to the configured default for a fresh install.
    """
    try:
        cur.execute(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'chunks' AND column_name = 'embedding'"
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 — a probe failure must not stop a connection
        return None
    if not row or row[0] not in SUPPORTED_VECTOR_TYPES:
        return None
    return str(row[0])


def detect_vector_type(cur) -> str:
    """Read the embedding column's actual type, or ``vector`` if unknowable.

    pgvector has no ``halfvec <=> vector`` operator, so a cast that disagrees
    with the column does not lose precision — it fails to resolve, and every
    vector search errors. The cast must follow the column, and the column is
    whatever a past migration left.

    Falls back to ``vector`` for a table that does not exist yet (a fresh
    install ensures the schema before there is anything to probe), for a type
    nobody planned for, and for a probe that raises. A detection failure must
    not stop a connection.
    """
    return existing_vector_type(cur) or "vector"


def schema_sql(vector_type: str | None = None) -> str:
    """The schema DDL, declaring *vector_type* for the embedding column.

    The default is deliberately still ``vector``. halfvec is measurably free on
    the corpus tested, but changing what every new install creates is a
    decision somebody makes on purpose, not a side effect of this function
    gaining a parameter.
    """
    vt = vector_type or os.environ.get(VECTOR_TYPE_ENV, "").strip() or "vector"
    if vt not in SUPPORTED_VECTOR_TYPES:
        raise ValueError(
            f"unsupported embedding column type {vt!r}; expected one of {SUPPORTED_VECTOR_TYPES}"
        )
    return _SCHEMA_SQL.replace("embedding       vector(768)", f"embedding       {vt}(768)").replace(
        "vector_cosine_ops", opclass_for(vt)
    )


def recommended_lists(n_chunks: int) -> int:
    """Recommended ivfflat ``lists`` for a corpus of ``n_chunks`` (~√N,
    the standard heuristic). Floored at 100 (the small-corpus default)."""
    import math

    if n_chunks <= 0:
        return 100
    return max(100, round(math.sqrt(n_chunks)))


def recommended_probes(n_chunks: int) -> int:
    """Recommended ivfflat ``probes`` scaled to corpus size. Recall ≈
    probes/lists; we target ~√N/20, clamped to [10, 200], so recall stays
    usable as the corpus grows without a hand-tuned ALTER DATABASE."""
    import math

    if n_chunks <= 0:
        return 10
    return max(10, min(200, round(math.sqrt(n_chunks) / 20)))


def probes_default_sql(database: str, n_chunks: int) -> str:
    """``ALTER DATABASE`` statement setting a sane ``ivfflat.probes`` default.

    Measured on a live 2.1M-chunk corpus: at pgvector's session default of
    ``probes = 1`` the index returned **0.5700** recall@10 against exact; at 72
    it returned 0.9167. :meth:`RAGStore._set_probes` sets it per connection, so
    the platform's own path was never affected — but psql, a notebook, a
    sibling service or a future extension querying the same table got 57% of
    the right answers, with no error and entirely plausible output.

    A default nobody has to know about is worth more than a helper only the
    platform remembers to call. A caller that knows better still overrides it
    per session.

    ``ALTER DATABASE`` accepts no bound parameters, so the name is interpolated
    as a quoted identifier and a name that cannot be quoted safely is refused
    rather than emitted.
    """
    if '"' in database or "\x00" in database:
        raise ValueError(f"refusing to build ALTER DATABASE for an unquotable name: {database!r}")
    return f'ALTER DATABASE "{database}" SET ivfflat.probes = {recommended_probes(n_chunks)}'


class RAGStore:
    """PostgreSQL/pgvector document store for RAG retrieval — three-tier corpus."""

    #: Embedding column type, re-read from the database on every connect.
    #:
    #: A CLASS attribute, not only an instance one: tests and subclasses build
    #: stores without running ``__init__``, and a missing attribute here fails
    #: inside ``search()`` — at query time, against a real corpus, which is the
    #: worst place to discover it.
    _vector_type: str = "vector"

    def __init__(self, database_url: str, *, ensure_schema: bool = True) -> None:
        self._dsn = database_url
        self._conn: psycopg2.extensions.connection | None = None
        # When False, connect() opens the connection but skips the schema DDL.
        # Concurrent ingest workers each hold their OWN connection (psycopg2
        # connections aren't thread-safe) and must NOT race on the
        # `CREATE INDEX`/`ALTER` DDL — that takes AccessExclusiveLock and
        # deadlocks between connections. The caller ensures the schema ONCE on
        # a primary store, then spawns workers with ensure_schema=False.
        self._ensure_schema = ensure_schema
        # Whether ``rag_generation_config`` exists; set on connect(). Without it
        # every corpus serves generation 1 (#1945).
        self._has_generation_config = False
        # Whether the per-generation embedding-model declarations exist; set on connect().
        self._has_embedding_declarations = False

    # -- connection management ------------------------------------------------

    def connect(self) -> None:
        """Establish a database connection (and ensure the schema unless the
        store was created with ``ensure_schema=False``)."""
        if self._conn and not self._conn.closed:
            return
        self._conn = psycopg2.connect(self._dsn)
        self._conn.autocommit = True
        # pgvector defaults ivfflat.probes to 1 → near-zero recall on a large
        # corpus (the retriever must own this, not the DB default). Scale it to
        # the corpus size on every connection (workers included). reltuples is a
        # cheap planner estimate — no full count.
        self._set_probes()
        if not self._ensure_schema:
            with self._conn.cursor() as cur:
                self._vector_type = detect_vector_type(cur)
                self._has_generation_config = _has_generation_config(cur)
                self._has_embedding_declarations = _has_generation_config(cur, EMBEDDING_MODEL_TABLE)
            return
        with self._conn.cursor() as cur:
            # Schema migration runs ALTER TABLE, which takes AccessExclusiveLock.
            # With no bound, a blocked ALTER waits forever on a concurrent reader
            # AND stalls the lock queue behind it — every later query then hangs
            # (observed 2026-06-08: a wedged migration froze all RAG reads). Bound
            # the lock wait so a contended migration fails fast instead of wedging.
            # lock_timeout also bounds the advisory-lock wait below.
            cur.execute("SET lock_timeout = '10s'")
            # Serialize the whole ensure across connections/processes: the DDL
            # script touches both `chunks` and `documents`, and two connections
            # running it concurrently deadlock on interleaved AccessExclusive
            # acquisition. Session-level lock (the connection is autocommit);
            # released in the finally so a failed ensure never wedges peers.
            cur.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_ADVISORY_LOCK_KEY,))
            try:
                # An existing column's type wins over the configured default:
                # the DDL's index opclass must match the column that is there.
                try:
                    cur.execute(schema_sql(existing_vector_type(cur)))
                except psycopg2.errors.InsufficientPrivilege as exc:
                    # A read-only role (a serving user with SELECT only) cannot run the DDL:
                    # say which setting a writer needs, not the raw driver error (#2100).
                    raise _schema_refused(exc) from None
                self._vector_type = detect_vector_type(cur)
                self._ensure_probes_default(cur)
                # Ensure generation infrastructure tables exist. This also renames a
                # legacy generation-quality `retrieval_log` to `rag_generation_log`
                # (P2b) before creating anything — see axiom.rag.generation.
                try:
                    from axiom.rag.generation import ensure_generation_schema

                    ensure_generation_schema(cur)
                except Exception:
                    pass  # Module unavailable, or a contended rename: retried on next connect()
                # Ensure interaction log table exists
                try:
                    from axiom.rag.interaction_log import ensure_interaction_log

                    ensure_interaction_log(self._conn)
                except Exception:
                    pass
                # Ensure retrieval audit table exists (T0-1)
                try:
                    from axiom.rag.retrieval_audit import ensure_retrieval_audit

                    ensure_retrieval_audit(self._conn)
                except Exception:
                    pass
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_ADVISORY_LOCK_KEY,))
            self._has_generation_config = _has_generation_config(cur)
            self._has_embedding_declarations = _has_generation_config(cur, EMBEDDING_MODEL_TABLE)
        log.info("RAGStore connected and schema ensured")

    def schema_behind(self) -> dict[str, list[str]]:
        """Columns the installed code declares that the store lacks, by table; ``{}`` when current.

        Read-only (``information_schema``), so a SELECT-only role can ask. A store behind the code
        is brought up to date by the next connect with ``ensure_schema=True`` under the owner role.
        """
        self.connect()
        wanted = expected_columns()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
                (list(wanted),),
            )
            have: dict[str, set[str]] = {}
            for table, col in cur.fetchall():
                have.setdefault(table, set()).add(col)
        return {
            table: missing
            for table, cols in wanted.items()
            if table in have and (missing := [c for c in cols if c not in have[table]])
        }

    def _ensure_probes_default(self, cur) -> None:
        """Give the DATABASE a sane ``ivfflat.probes``, once, at schema-ensure.

        ``_set_probes`` already covers this store's own connections. This
        covers everyone else: psql, a notebook, a sibling service, a future
        extension. On the live corpus the difference between the pgvector
        default and the recommended value was **0.5700 versus 0.9167** recall,
        with no error to indicate anything was wrong.

        **Best effort.** ``ALTER DATABASE`` needs ownership, and a deployment
        that connects as a restricted role is a legitimate configuration, not a
        failure — it just keeps the old default and the per-connection setting
        still applies. Never raises.

        Only writes when the recorded default is missing or has drifted, so a
        reconnect is not a write.
        """
        try:
            cur.execute("SELECT current_database()")
            dbname = cur.fetchone()[0]
            cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'chunks'")
            row = cur.fetchone()
            n = max(int(row[0]), 0) if row and row[0] is not None else 0
            want = recommended_probes(n)

            cur.execute(
                "SELECT split_part(s, '=', 2) FROM pg_db_role_setting r, "
                "unnest(r.setconfig) s JOIN pg_database d ON d.oid = r.setdatabase "
                "WHERE d.datname = current_database() AND s LIKE 'ivfflat.probes=%'"
            )
            current = cur.fetchone()
            if current and str(current[0]).strip() == str(want):
                return

            cur.execute(probes_default_sql(dbname, n))
            log.info(
                "Set database default ivfflat.probes=%d for %s "
                "(was %s) — protects callers that do not set it themselves",
                want,
                dbname,
                current[0] if current else "unset",
            )
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.debug("Could not set a database-level ivfflat.probes default: %s", exc)

    def _set_probes(self) -> None:
        """Set ``ivfflat.probes`` for this connection, scaled to corpus size.

        Recall on an ivfflat index is roughly ``probes / lists``; with the
        pgvector default of 1 probe, recall collapses as the corpus grows.
        We scale probes with the corpus (≈ ``√N / 20``, clamped) so retrieval
        stays usable without a hand-tuned ``ALTER DATABASE``."""
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'chunks'")
                row = cur.fetchone()
                n = int(row[0]) if row and row[0] and row[0] > 0 else 0
                cur.execute(f"SET ivfflat.probes = {recommended_probes(n)}")
        except Exception:  # noqa: BLE001 — never let tuning break connect()
            pass

    def _cur(self):
        """Return a cursor, reconnecting if needed."""
        if self._conn is None or self._conn.closed:
            self.connect()
        assert self._conn is not None
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    # -- write operations -----------------------------------------------------

    def upsert_chunks(
        self,
        chunks: list[Chunk],
        embeddings: list[list[float]] | None = None,
        checksum: str = "",
        content_hash: str = "",
        corpus: str = CORPUS_INTERNAL,
        owner: str | None = None,
        chunking_tier: str = "fixed",
        data_source: str = "local",
        sync_id: str = "",
        corpus_generation: int | None = None,
        cognitive_type: str | None = None,
        fragment_ref: str | None = None,
        source_url: str | None = None,
        source_ref_id: str | None = None,
        access_tier: str | None = None,
        classification: str | None = None,
        allowed_nationalities: tuple[str, ...] | list[str] | None = None,
        parser: dict | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Insert or replace all chunks for a document within a corpus generation.

        All chunks must share the same ``source_path``.  Existing chunks for
        that path+corpus in the same generation are deleted first (full
        replace); other generations' chunks are left alone, so building a
        candidate never removes what is being served.  ``corpus_generation``
        defaults to the generation the corpus serves (:meth:`served_generation`),
        so ordinary ingest lands where retrieval reads.  Embeddings are
        optional — if None, chunks are stored with NULL embeddings (full-text
        search only).

        The access labels are written to every chunk and to the document.
        A tier the retrieval filter cannot read is refused before anything is
        written (#1971). A call that declares no label at all keeps the
        labels the document already has (a re-index must not reset a label a
        person or a backfill set), and a new document starts public and
        unclassified.

        The document keeps ``parser`` (how it was parsed: the extractor(s) and
        their versions). Like ``source_url``, a re-index that does not pass it
        keeps what an earlier ingest captured.

        ``embedding_model`` names the model that made ``embeddings``. The first
        such write declares it for the corpus generation; vectors from any other
        model are refused (:class:`EmbeddingModelMismatch`) before anything is
        written. A caller that names no model is accepted as before.
        """
        if not chunks:
            return

        source_path = chunks[0].source_path
        now = datetime.now(UTC)
        generation = (
            int(corpus_generation) if corpus_generation is not None else self.served_generation(corpus)
        )

        undeclared = access_tier is None and classification is None and allowed_nationalities is None
        if not undeclared:
            nats = check_access_labels(
                access_tier or "public", classification or "unclassified", allowed_nationalities
            )

        with self._cur() as cur:
            if undeclared:
                cur.execute(
                    "SELECT access_tier, classification, allowed_nationalities FROM documents "
                    "WHERE source_path = %s AND corpus = %s",
                    (source_path, corpus),
                )
                row = cur.fetchone()
                stored = (
                    (row["access_tier"], row["classification"], row["allowed_nationalities"])
                    if row
                    else DEFAULT_LABELS
                )
                access_tier, classification = stored[0], stored[1]
                nats = list(stored[2]) if stored[2] else None
            access_tier = access_tier or "public"
            classification = classification or "unclassified"

            if embeddings and embedding_model:
                self._declare_embedding_model(cur, corpus, generation, embedding_model, source_path)

            # Delete old chunks for this path+corpus, in this generation only
            cur.execute(
                "DELETE FROM chunks WHERE source_path = %s AND corpus = %s AND corpus_generation = %s",
                (source_path, corpus, generation),
            )

            for i, chunk in enumerate(chunks):
                emb = embeddings[i] if embeddings and i < len(embeddings) else None
                emb_val = str(emb) if emb else None
                cur.execute(
                    """
                    INSERT INTO chunks
                        (source_path, source_title, source_type, chunk_text,
                         chunk_index, start_line, embedding, corpus, owner,
                         checksum, indexed_at, updated_at,
                         cognitive_type, fragment_ref, corpus_generation,
                         access_tier, classification, allowed_nationalities)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk.source_path,
                        clean_title(chunk.source_title, chunk.source_path),
                        chunk.source_type,
                        chunk.text,
                        chunk.chunk_index,
                        chunk.start_line,
                        emb_val,
                        corpus,
                        owner,
                        checksum,
                        now,
                        now,
                        cognitive_type,
                        fragment_ref,
                        generation,
                        access_tier,
                        classification,
                        nats,
                    ),
                )

            # Upsert documents record
            cur.execute(
                """
                INSERT INTO documents
                    (source_path, corpus, source_type, title, checksum,
                     chunk_count, owner, first_indexed, last_indexed,
                     source_url, source_ref_id,
                     access_tier, classification, allowed_nationalities, parser,
                     corpus_generation)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_path, corpus) DO UPDATE SET
                    corpus_generation = EXCLUDED.corpus_generation,
                    access_tier   = EXCLUDED.access_tier,
                    classification = EXCLUDED.classification,
                    allowed_nationalities = EXCLUDED.allowed_nationalities,
                    title         = EXCLUDED.title,
                    checksum      = EXCLUDED.checksum,
                    chunk_count   = EXCLUDED.chunk_count,
                    owner         = EXCLUDED.owner,
                    last_indexed  = EXCLUDED.last_indexed,
                    -- ADR-091: a URL-less re-index (e.g. local re-embed) must not
                    -- wipe a link a prior ingest/backfill already captured.
                    source_url    = COALESCE(EXCLUDED.source_url, documents.source_url),
                    source_ref_id = COALESCE(EXCLUDED.source_ref_id, documents.source_ref_id),
                    parser        = COALESCE(EXCLUDED.parser, documents.parser)
                """,
                (
                    source_path,
                    corpus,
                    chunks[0].source_type,
                    chunks[0].source_title,
                    checksum,
                    len(chunks),
                    owner,
                    now,
                    now,
                    source_url,
                    source_ref_id,
                    access_tier,
                    classification,
                    nats,
                    psycopg2.extras.Json(parser) if parser else None,
                    generation,
                ),
            )

    def _declare_embedding_model(self, cur, corpus: str, generation: int, model: str, source_path: str) -> None:
        """Declare ``model`` for the corpus generation if nothing has; refuse it if another is declared."""
        if not getattr(self, "_has_embedding_declarations", False):
            return
        cur.execute(
            f"INSERT INTO {EMBEDDING_MODEL_TABLE} (corpus, corpus_generation, embedding_model) "
            "VALUES (%s, %s, %s) ON CONFLICT (corpus, corpus_generation) DO NOTHING",
            (corpus, generation, model),
        )
        cur.execute(
            f"SELECT embedding_model FROM {EMBEDDING_MODEL_TABLE} WHERE corpus = %s AND corpus_generation = %s",
            (corpus, generation),
        )
        row = cur.fetchone()
        declared = row["embedding_model"] if row else model
        if declared != model:
            raise EmbeddingModelMismatch(mismatch_message(corpus, declared, model, source_path, generation))

    def declared_embedding_models(
        self, corpora: list[str] | None = None, *, corpus_generation: int | None = None
    ) -> dict[str, str]:
        """The embedding model each corpus declared for the generation it serves (or the one named).

        ``None`` names every corpus. A corpus that declared nothing is absent. Read-only.
        """
        if not getattr(self, "_has_embedding_declarations", False):
            return {}
        if corpus_generation is not None:
            gen_sql = "m.corpus_generation = %s"
            params: list = [int(corpus_generation)]
        elif getattr(self, "_has_generation_config", False):
            gen_sql = (
                "m.corpus_generation = COALESCE((SELECT g.active_generation FROM rag_generation_config g "
                "WHERE g.corpus = m.corpus), 1)"
            )
            params = []
        else:
            gen_sql = "m.corpus_generation = 1"
            params = []
        sql = f"SELECT m.corpus, m.embedding_model FROM {EMBEDDING_MODEL_TABLE} m WHERE {gen_sql}"
        if corpora is not None:
            sql += " AND m.corpus = ANY(%s)"
            params.append(list(corpora))
        with self._cur() as cur:
            cur.execute(sql, params)
            return {r["corpus"]: r["embedding_model"] for r in cur.fetchall()}

    def vectorless_chunk_counts(
        self, corpora: list[str] | None = None, *, corpus_generation: int | None = None
    ) -> dict[str, int]:
        """Per corpus, the chunks of the served generation stored without a vector. Read-only.

        These are text-only: no embedder was configured, or none was admitted for their label
        (P0.2). ``embed_awaiting`` gives them vectors once one is. A corpus with none is absent.
        """
        sql = (
            "SELECT corpus, count(*) AS n FROM chunks WHERE embedding IS NULL "
            f"AND {self._generation_sql(corpus_generation)}"
        )
        params: list = []
        if corpora is not None:
            sql += " AND corpus = ANY(%s)"
            params.append(list(corpora))
        with self._cur() as cur:
            cur.execute(sql + " GROUP BY corpus", params)
            return {r["corpus"]: int(r["n"]) for r in cur.fetchall()}

    def chunks_awaiting_embedding(
        self, corpus: str, *, after_id: int = 0, limit: int = 100, corpus_generation: int | None = None
    ) -> list[dict]:
        """The next ``limit`` vectorless chunks of ``corpus`` after ``after_id``: id, text and label."""
        with self._cur() as cur:
            cur.execute(
                "SELECT id, chunk_text, access_tier FROM chunks WHERE corpus = %s AND embedding IS NULL "
                f"AND id > %s AND {self._generation_sql(corpus_generation)} ORDER BY id LIMIT %s",
                (corpus, int(after_id), int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def set_chunk_embeddings(
        self, vectors: dict[int, list[float]], *, corpus: str | None = None, embedding_model: str | None = None
    ) -> int:
        """Give stored chunks their vectors, by chunk id. Only a chunk still without one is written.

        With ``corpus`` and ``embedding_model``, the model is held to the one the corpus's served
        generation recorded (recorded here when there is none); another is refused
        (:class:`EmbeddingModelMismatch`) before anything is written.
        """
        written = 0
        with self._cur() as cur:
            if vectors and corpus and embedding_model:
                self._declare_embedding_model(
                    cur, corpus, self.served_generation(corpus), embedding_model, "(chunks awaiting a vector)"
                )
            for chunk_id, vec in vectors.items():
                cur.execute(
                    "UPDATE chunks SET embedding = %s, updated_at = now() WHERE id = %s AND embedding IS NULL",
                    (str(vec), int(chunk_id)),
                )
                written += cur.rowcount or 0
        return written

    def record_embedding_model(self, corpus: str, corpus_generation: int, model: str) -> str:
        """Record ``model`` for the corpus generation unless one is; returns the recorded model."""
        if not getattr(self, "_has_embedding_declarations", False):
            raise RuntimeError(f"{EMBEDDING_MODEL_TABLE} does not exist here; connect with schema ensure first")
        with self._cur() as cur:
            cur.execute(
                f"INSERT INTO {EMBEDDING_MODEL_TABLE} (corpus, corpus_generation, embedding_model) "
                "VALUES (%s, %s, %s) ON CONFLICT (corpus, corpus_generation) DO NOTHING",
                (corpus, int(corpus_generation), model),
            )
            cur.execute(
                f"SELECT embedding_model FROM {EMBEDDING_MODEL_TABLE} WHERE corpus = %s AND corpus_generation = %s",
                (corpus, int(corpus_generation)),
            )
            return cur.fetchone()["embedding_model"]

    def sample_embedded_chunks(
        self, corpus: str, *, n: int = 20, corpus_generation: int | None = None
    ) -> list[dict]:
        """Up to ``n`` stored chunks with a vector, one per document, from documents chosen at random.

        Each row: ``id``, ``source_path``, ``chunk_text``, ``access_tier`` and ``embedding`` (a list).
        """
        with self._cur() as cur:
            cur.execute(
                "SELECT * FROM (SELECT DISTINCT ON (source_path) id, source_path, chunk_text, access_tier, "
                "embedding::text AS embedding FROM chunks WHERE corpus = %s AND embedding IS NOT NULL "
                f"AND {self._generation_sql(corpus_generation)} ORDER BY source_path, chunk_index) per_doc "
                "ORDER BY random() LIMIT %s",
                (corpus, int(n)),
            )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["embedding"] = [float(x) for x in str(r["embedding"]).strip("[]").split(",") if x.strip()]
        return rows

    def corpora_without_a_recorded_model(self) -> list[str]:
        """Corpora whose served generation holds vectors but recorded no embedding model. Read-only."""
        if not getattr(self, "_has_embedding_declarations", False):
            return []
        served = self._generation_sql(None)
        recorded_gen = (
            "COALESCE((SELECT g.active_generation FROM rag_generation_config g WHERE g.corpus = d.corpus), 1)"
            if getattr(self, "_has_generation_config", False)
            else "1"
        )
        with self._cur() as cur:
            cur.execute(
                "SELECT d.corpus FROM (SELECT DISTINCT corpus FROM documents) d "
                "WHERE EXISTS (SELECT 1 FROM chunks WHERE chunks.corpus = d.corpus "
                f"AND chunks.embedding IS NOT NULL AND {served}) "
                f"AND NOT EXISTS (SELECT 1 FROM {EMBEDDING_MODEL_TABLE} m WHERE m.corpus = d.corpus "
                f"AND m.corpus_generation = {recorded_gen}) ORDER BY d.corpus"
            )
            return [r["corpus"] for r in cur.fetchall()]

    def served_generation(self, corpus: str) -> int:
        """The generation retrieval reads for ``corpus``: its active generation, or 1.

        Read-only: unlike ``GenerationManager.get_active_generation`` it never
        creates a config row.
        """
        if not getattr(self, "_has_generation_config", False):
            return 1
        with self._cur() as cur:
            cur.execute(
                "SELECT active_generation FROM rag_generation_config WHERE corpus = %s",
                (corpus,),
            )
            row = cur.fetchone()
        if not row:
            return 1
        value = row["active_generation"] if isinstance(row, dict) else row[0]
        return int(value) if value is not None else 1

    def generation_report(self) -> list[dict]:
        """Per corpus: the generation it serves, its candidate, and its chunks by generation.

        Read-only. ``hidden`` counts the chunks retrieval does not read because
        they are in another generation: a candidate under evaluation, an old
        generation kept for rollback, or chunks left behind by a build that was
        never promoted or discarded.
        """
        with self._cur() as cur:
            cur.execute(
                "SELECT corpus, corpus_generation, count(*) AS n FROM chunks "
                "GROUP BY corpus, corpus_generation ORDER BY corpus, corpus_generation"
            )
            counts = cur.fetchall()
            config: dict[str, tuple[int, int | None]] = {}
            if getattr(self, "_has_generation_config", False):
                cur.execute("SELECT corpus, active_generation, candidate_generation FROM rag_generation_config")
                for r in cur.fetchall():
                    config[r["corpus"]] = (int(r["active_generation"]), r["candidate_generation"])
        by_corpus: dict[str, dict[int, int]] = {}
        for r in counts:
            by_corpus.setdefault(r["corpus"], {})[int(r["corpus_generation"])] = int(r["n"])
        out = []
        for corpus in sorted(set(by_corpus) | set(config)):
            served, candidate = config.get(corpus, (1, None))
            gens = by_corpus.get(corpus, {})
            out.append(
                {
                    "corpus": corpus,
                    "served_generation": served,
                    "candidate_generation": candidate,
                    "chunks_by_generation": gens,
                    "served": gens.get(served, 0),
                    "hidden": sum(n for g, n in gens.items() if g != served),
                }
            )
        return out

    def delete_document(self, path: str, corpus: str = CORPUS_INTERNAL) -> None:
        """Remove all chunks and the document record for *path* in *corpus*."""
        with self._cur() as cur:
            cur.execute(
                "DELETE FROM chunks WHERE source_path = %s AND corpus = %s",
                (path, corpus),
            )
            cur.execute(
                "DELETE FROM documents WHERE source_path = %s AND corpus = %s",
                (path, corpus),
            )

    def document_labels(self, path: str, corpus: str) -> tuple[str, str] | None:
        """The ``(access_tier, classification)`` a document is stored with, or ``None``."""
        with self._cur() as cur:
            cur.execute(
                "SELECT access_tier, classification FROM documents WHERE source_path = %s AND corpus = %s",
                (path, corpus),
            )
            row = cur.fetchone()
        return (row["access_tier"], row["classification"]) if row else None

    def set_document_labels(self, path: str, corpus: str, *, access_tier: str, classification: str) -> int:
        """Relabel a document and every chunk of it (the #1971 backfill). Returns chunks relabelled."""
        check_access_labels(access_tier, classification, None)
        with self._cur() as cur:
            cur.execute(
                "UPDATE chunks SET access_tier = %s, classification = %s WHERE source_path = %s AND corpus = %s",
                (access_tier, classification, path, corpus),
            )
            n = cur.rowcount or 0
            cur.execute(
                "UPDATE documents SET access_tier = %s, classification = %s WHERE source_path = %s AND corpus = %s",
                (access_tier, classification, path, corpus),
            )
        return int(n)

    def set_document_classification(
        self, path: str, classification: str, corpus: str = CORPUS_INTERNAL
    ) -> int:
        """Stamp the export-control label on every chunk of *path* in *corpus* (MVP-06-A1).

        The retriever reads ``classification`` off each chunk, so the label a
        person confirmed is enforced at retrieval without a second lookup.
        Returns the number of chunks stamped.
        """
        with self._cur() as cur:
            cur.execute(
                "UPDATE chunks SET classification = %s WHERE source_path = %s AND corpus = %s",
                (classification, path, corpus),
            )
            n = cur.rowcount or 0
            cur.execute(
                "UPDATE documents SET classification = %s WHERE source_path = %s AND corpus = %s",
                (classification, path, corpus),
            )
        return int(n)

    def delete_corpus(self, corpus: str) -> int:
        """Remove all chunks and documents for an entire corpus. Returns chunk count."""
        with self._cur() as cur:
            cur.execute("SELECT count(*) AS n FROM chunks WHERE corpus = %s", (corpus,))
            row = cur.fetchone()
            assert row is not None
            n = row["n"]
            cur.execute("DELETE FROM chunks WHERE corpus = %s", (corpus,))
            cur.execute("DELETE FROM documents WHERE corpus = %s", (corpus,))
        log.info("Deleted corpus %s (%d chunks)", corpus, n)
        return n

    # -- read operations ------------------------------------------------------

    def get_document(self, path: str, corpus: str = CORPUS_INTERNAL) -> dict | None:
        """Return document metadata or ``None`` if not indexed."""
        with self._cur() as cur:
            cur.execute(
                "SELECT source_path, corpus, checksum, content_hash, chunk_count, last_indexed, "
                "source_url, source_ref_id, parser, corpus_generation "
                "FROM documents WHERE source_path = %s AND corpus = %s",
                (path, corpus),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def document_parsers(self, corpus: str) -> dict[str, dict | None]:
        """Every document of ``corpus`` and its parser record (``None`` when none was
        recorded), so a re-index can tell which documents a new extractor changes."""
        with self._cur() as cur:
            cur.execute("SELECT source_path, parser FROM documents WHERE corpus = %s", (corpus,))
            return {r["source_path"]: r["parser"] for r in cur.fetchall()}

    def access_tier_for(self, source_path: str) -> str | None:
        """The access tier for a retrieved source path, or ``None`` if unknown.

        The retriever's access filter needs each chunk's real tier (its
        ``SearchResult`` does not carry it). Returns the MOST restrictive tier
        across the path's chunks, so a mixed-tier path is treated at its
        tightest. ``None`` means the path isn't indexed — callers MUST fail
        closed (treat as non-public), never default it to public.
        """
        with self._cur() as cur:
            cur.execute(
                # tightest tier wins: 'export_controlled' > 'restricted' > 'public'
                "SELECT access_tier FROM chunks WHERE source_path = %s "
                "ORDER BY CASE access_tier "
                "  WHEN 'export_controlled' THEN 0 WHEN 'restricted' THEN 1 ELSE 2 END "
                "LIMIT 1",
                (source_path,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return row["access_tier"] if isinstance(row, dict) else row[0]

    def find_documents_by_name(self, name: str, corpus: str = CORPUS_INTERNAL) -> list[dict]:
        """Resolve a name-or-path to matching documents in *corpus*.

        If ``name`` contains a ``/``, treat as an exact ``source_path``
        match. Otherwise, basename-match (``source_path = name`` or
        ``source_path LIKE '%/<name>'``). Returns a list of
        ``{source_path, chunk_count}`` dicts so the caller can show
        candidates + sizes before deleting. See ``axi rag remove``.
        """
        with self._cur() as cur:
            if "/" in name:
                cur.execute(
                    "SELECT source_path, chunk_count "
                    "FROM documents WHERE source_path = %s AND corpus = %s",
                    (name, corpus),
                )
            else:
                # Match either an exact bare-filename row OR any path
                # whose final component equals ``name``. The trailing-
                # slash pattern keeps ``foo.pdf`` from matching
                # ``otherfoo.pdf``.
                like = f"%/{name}"
                cur.execute(
                    "SELECT source_path, chunk_count "
                    "FROM documents "
                    "WHERE corpus = %s AND (source_path = %s OR source_path LIKE %s) "
                    "ORDER BY source_path",
                    (corpus, name, like),
                )
            return [dict(row) for row in cur.fetchall()]

    def list_document_paths(self, corpus: str = CORPUS_INTERNAL) -> list[str]:
        """Return all indexed source paths in *corpus* (for auditing / purge)."""
        with self._cur() as cur:
            cur.execute(
                "SELECT source_path FROM documents WHERE corpus = %s ORDER BY source_path",
                (corpus,),
            )
            return [row["source_path"] for row in cur.fetchall()]

    def find_by_content_hash(self, content_hash: str) -> list[dict]:
        """Find all documents with the same extracted-text content hash."""
        if not content_hash:
            return []
        with self._cur() as cur:
            cur.execute(
                "SELECT source_path, corpus, checksum, content_hash, chunk_count, last_indexed "
                "FROM documents WHERE content_hash = %s",
                (content_hash,),
            )
            return [dict(r) for r in cur.fetchall()]

    def search(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        corpora: list[str] | None = None,
        limit: int = 5,
        chunking_tier: str | None = None,
        corpus_generation: int | None = None,
        tsquery: str | None = None,
    ) -> list[SearchResult]:
        """Hybrid vector + full-text search across one or more corpora.

        ``tsquery``, when given, is the text ranking's query as is (a precise
        ranking a caller built, such as one requiring a named identifier),
        instead of the OR of ``query_text``'s terms.

        Results are ranked by score, not by corpus: there is no corpus
        precedence in the SQL and a caller that needs one must impose it.
        If ``corpora`` is None, every corpus is searched, which on a served
        surface is a disclosure — name the corpora instead.
        If no embeddings (query_embedding is None), falls back to pure
        full-text search over tsvector.

        Each corpus is read at the generation it serves (its active generation,
        or 1), so a candidate built by ``axi rag upgrade`` is invisible until it
        is promoted. ``corpus_generation`` names one generation instead, for an
        evaluation of a candidate (#1945).
        """
        if corpora is None:
            corpora = list(ALL_CORPORA)

        corpus_filter = tuple(corpora)
        params: list = []

        # OR-of-terms recall (websearch_to_tsquery ANDs every term -> zero recall
        # on a multi-term query -> silent dense-only collapse). Empty when the
        # query carries no usable terms; the text backend is then skipped.
        tsq = tsquery.strip() if tsquery else _fts_tsquery(query_text)
        has_text = bool(tsq)

        emb_str = str(query_embedding) if query_embedding is not None else ""
        entq = _fts_entity_tsquery(query_text) if has_text and not tsquery else ""

        if query_embedding is not None and has_text and entq and entq != tsq:
            # The query names a discriminative identifier (acronym/model number) that
            # is drowned by high-frequency generic terms: dense similarity buries its
            # chunk under generic prose, and scoping FTS to the vector candidates never
            # recovers it. Recall it independently in entity_search, union it into the
            # pool, and float the matched chunks with a small boost. Only queries that
            # name an identifier take this path; all others keep the plain hybrid below.
            sql = """
                WITH vector_search AS (
                    SELECT id, source_path, source_title, chunk_text,
                           chunk_index, corpus,
                           1 - (embedding <=> %s::{vec}) AS similarity
                    FROM chunks
                    WHERE corpus = ANY(%s) AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::{vec}
                    LIMIT %s
                ),
                entity_search AS (
                    SELECT id, source_path, source_title, chunk_text,
                           chunk_index, corpus,
                           1 - (embedding <=> %s::{vec}) AS similarity
                    FROM chunks
                    WHERE id IN (
                        SELECT id FROM chunks
                        WHERE corpus = ANY(%s)
                          AND to_tsvector('english', chunk_text) @@
                              to_tsquery('english', %s)
                        LIMIT %s
                    )
                    ORDER BY embedding <=> %s::{vec}
                    LIMIT %s
                ),
                pool AS (
                    SELECT id, source_path, source_title, chunk_text,
                           chunk_index, corpus, similarity FROM vector_search
                    UNION
                    SELECT id, source_path, source_title, chunk_text,
                           chunk_index, corpus, similarity FROM entity_search
                ),
                text_search AS (
                    SELECT id,
                           ts_rank(to_tsvector('english', chunk_text),
                                   to_tsquery('english', %s)) AS text_rank
                    FROM chunks
                    WHERE id IN (SELECT id FROM pool)
                      AND to_tsvector('english', chunk_text) @@
                          to_tsquery('english', %s)
                )
                SELECT p.source_path, p.source_title, p.chunk_text,
                       p.chunk_index, p.corpus, p.similarity,
                       (0.7 * p.similarity + 0.3 * COALESCE(t.text_rank, 0)
                        + 0.1 * (CASE WHEN p.id IN (SELECT id FROM entity_search)
                                 THEN 1 ELSE 0 END)) AS combined_score,
                       acc.access_tier, acc.classification, acc.allowed_nationalities
                FROM pool p
                LEFT JOIN text_search t ON p.id = t.id
                LEFT JOIN chunks acc ON acc.id = p.id
                ORDER BY combined_score DESC
                LIMIT %s
            """
            params = [
                emb_str,
                list(corpus_filter),
                emb_str,
                limit * 2,
                emb_str,
                list(corpus_filter),
                entq,
                TEXT_SCAN_CAP,
                emb_str,
                limit * 2,
                tsq,
                tsq,
                limit,
            ]
        elif query_embedding is not None and has_text:
            # Hybrid. text_search scores ONLY the vector candidates (id IN
            # vector_search), so ts_rank never ranks the whole corpus.
            sql = """
                WITH vector_search AS (
                    SELECT id, source_path, source_title, chunk_text,
                           chunk_index, corpus,
                           1 - (embedding <=> %s::{vec}) AS similarity
                    FROM chunks
                    WHERE corpus = ANY(%s) AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::{vec}
                    LIMIT %s
                ),
                text_search AS (
                    SELECT id,
                           ts_rank(to_tsvector('english', chunk_text),
                                   to_tsquery('english', %s)) AS text_rank
                    FROM chunks
                    WHERE id IN (SELECT id FROM vector_search)
                      AND to_tsvector('english', chunk_text) @@
                          to_tsquery('english', %s)
                )
                SELECT v.source_path, v.source_title, v.chunk_text,
                       v.chunk_index, v.corpus, v.similarity,
                       (0.7 * v.similarity + 0.3 * COALESCE(t.text_rank, 0))
                           AS combined_score,
                       acc.access_tier, acc.classification, acc.allowed_nationalities
                FROM vector_search v
                LEFT JOIN text_search t ON v.id = t.id
                LEFT JOIN chunks acc ON acc.id = v.id
                ORDER BY combined_score DESC
                LIMIT %s
            """
            params = [emb_str, list(corpus_filter), emb_str, limit * 2, tsq, tsq, limit]
        elif query_embedding is not None:
            sql = """
                SELECT source_path, source_title, chunk_text, chunk_index, corpus,
                       1 - (embedding <=> %s::{vec}) AS similarity,
                       1 - (embedding <=> %s::{vec}) AS combined_score,
                       access_tier, classification, allowed_nationalities
                FROM chunks
                WHERE corpus = ANY(%s) AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::{vec}
                LIMIT %s
            """
            emb_str = str(query_embedding)
            params = [emb_str, emb_str, list(corpus_filter), emb_str, limit]
        elif has_text:
            # Pure full-text search, scan-bounded: the inner query caps the match
            # scan at TEXT_SCAN_CAP (no ORDER), then the outer ranks that set, so
            # ORDER BY ts_rank never ranks every match of a broad OR query.
            sql = """
                SELECT source_path, source_title, chunk_text, chunk_index, corpus,
                       similarity, combined_score,
                       access_tier, classification, allowed_nationalities
                FROM (
                    SELECT source_path, source_title, chunk_text, chunk_index, corpus,
                           0.0 AS similarity,
                           ts_rank(to_tsvector('english', chunk_text),
                                   to_tsquery('english', %s)) AS combined_score,
                           access_tier, classification, allowed_nationalities
                    FROM chunks
                    WHERE corpus = ANY(%s)
                      AND to_tsvector('english', chunk_text) @@
                          to_tsquery('english', %s)
                    LIMIT %s
                ) hits
                ORDER BY combined_score DESC
                LIMIT %s
            """
            params = [tsq, list(corpus_filter), tsq, TEXT_SCAN_CAP, limit]
        else:
            return []

        with self._cur() as cur:
            if has_text:
                # The bounded text scan is `LIMIT n` with no ORDER BY, which the
                # planner answers with a sequential scan. Postgres defaults
                # `synchronize_seqscans = on`, letting a scan START MID-TABLE to
                # share buffer reads with a concurrent one; its own docs warn
                # this "can cause unpredictable changes in the row ordering
                # returned by queries that have no ORDER BY clause". So the same
                # question sampled a different arbitrary window every call, and
                # the answer's grounding changed underneath the asker.
                #
                # Pinning it here rather than adding an ORDER BY is deliberate:
                # ordering forces the scan to find all ~435k matches before it
                # can rank (measured ~10s), while the cap's whole speed comes
                # from stopping at the first n. Determinism costs ~1s -> ~4s on
                # the live corpus, because the scan can no longer join one
                # already in flight.
                #
                # Plain SET, not SET LOCAL. SET LOCAL lives only inside a
                # transaction, and this connection autocommits, so the setting
                # was discarded before the very next statement — it read as a
                # fix and did nothing. Verified against the live corpus: with
                # SET LOCAL the scan was still non-deterministic.
                #
                # Connection-scoped rather than server-wide. The only large
                # sequential scan on this connection is this one, so nothing
                # else loses the shared-read benefit.
                cur.execute("SET synchronize_seqscans = off")
            # {vec} follows the embedding column, detected on connect. pgvector
            # has no halfvec <=> vector operator, so a cast that disagrees with
            # the column errors rather than losing precision.
            gen = generation_predicate(
                corpus_generation, has_config=getattr(self, "_has_generation_config", False)
            )
            sql = sql.replace(_CORPUS_SCAN, f"{_CORPUS_SCAN} AND {gen}")
            cur.execute(sql.replace("{vec}", self._vector_type), params)
            rows = cur.fetchall()

        results = []
        for r in rows:
            chunk_text = r["chunk_text"]
            try:
                from axiom.infra.security_log import SecurityLog
                from axiom.infra.trace import current_session
                from axiom.rag.sanitizer import get_sanitizer

                clean_text, hits = get_sanitizer().sanitize(chunk_text)
                if hits:
                    SecurityLog.get().chunk_injection(
                        chunk_source=r["source_path"],
                        patterns_matched=hits,
                        session_id=current_session(),
                        corpus=r["corpus"],
                        sanitized=True,
                    )
                    chunk_text = clean_text
            except Exception:
                pass  # sanitization never blocks retrieval
            raw_nats = _row_val(r, "allowed_nationalities", None)
            results.append(
                SearchResult(
                    source_path=r["source_path"],
                    source_title=r["source_title"],
                    chunk_text=chunk_text,
                    chunk_index=r["chunk_index"],
                    similarity=float(r["similarity"]),
                    combined_score=float(r["combined_score"]),
                    corpus=r["corpus"],
                    # Access metadata projected by every search branch. A row that
                    # somehow lacks a column degrades to the fail-open default —
                    # exactly the pre-projection behaviour, never a hot-path crash.
                    access_tier=_row_val(r, "access_tier", "public"),
                    classification=_row_val(r, "classification", "unclassified"),
                    allowed_nationalities=tuple(raw_nats) if raw_nats else None,
                )
            )
        return results

    def name_ranking(
        self,
        name_tsquery: str,
        corpora: list[str] | None = None,
        *,
        max_sources: int,
        limit: int,
        exclude_path_re: str | None = None,
        query_embedding: list[float] | None = None,
        names_table: str | None = None,
        corpus_generation: int | None = None,
    ) -> list[SearchResult]:
        """Chunks of the sources whose NAME (title and path) matches ``name_tsquery`` (#1984).

        Chunk text is all the full-text index covers, so a document named only in its title or
        path cannot be found by name any other way. Returns ``[]`` when nothing matches or more
        than ``max_sources`` sources do (a topic, not a document). Sources whose path matches
        ``exclude_path_re`` (developer files) neither count nor return. The best ``limit``
        chunks come back nearest the query first when there is an embedding, else in document
        order. Names come from ``documents`` (title and path, folded to words) unless the site
        keeps an indexed table of them (``names_table``: ``corpus``, ``source_path``,
        ``name_tsv``). The names only narrow which sources; the chunks carry their access
        columns for the retriever's filter, as every search does. The chunks come from each
        corpus's served generation, or ``corpus_generation`` when named, as in :meth:`search`
        (#1945).
        """
        if not name_tsquery:
            return []
        if corpora is None:
            corpora = list(ALL_CORPORA)
        if names_table is not None and not re.fullmatch(r"[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?", names_table):
            raise ValueError(f"names_table is not a plain table name: {names_table!r}")
        if names_table:
            name_tsv, src = "name_tsv", names_table
        else:
            name_tsv = ("to_tsvector('english', regexp_replace(coalesce(title, '') || ' ' || source_path, "
                        "'[^A-Za-z0-9]+', ' ', 'g'))")
            src = "documents"
        exclude = " AND source_path !~ %s" if exclude_path_re else ""
        names_sql = (
            f"SELECT source_path FROM {src} WHERE corpus = ANY(%s) "
            f"AND {name_tsv} @@ to_tsquery('english', %s){exclude} "
            f"GROUP BY source_path LIMIT %s"
        )
        params: list[Any] = [list(corpora), name_tsquery]
        if exclude_path_re:
            params.append(exclude_path_re)
        params.append(max_sources + 1)
        cols = ("source_path, source_title, chunk_text, chunk_index, corpus, "
                "access_tier, classification, allowed_nationalities")
        gen = self._generation_sql(corpus_generation)
        with self._cur() as cur:
            cur.execute(names_sql, params)
            paths = sorted({r["source_path"] for r in cur.fetchall()})
            if not paths or len(paths) > max_sources:
                return []
            if query_embedding is not None:
                emb = str(query_embedding)
                cur.execute(
                    f"SELECT {cols}, 1 - (embedding <=> %s::{self._vector_type}) AS similarity FROM chunks "
                    f"WHERE source_path = ANY(%s) AND corpus = ANY(%s) AND {gen} AND embedding IS NOT NULL "
                    f"ORDER BY embedding <=> %s::{self._vector_type} LIMIT %s",
                    (emb, paths, list(corpora), emb, limit),
                )
            else:
                cur.execute(
                    f"SELECT {cols}, 0.0 AS similarity FROM chunks "
                    f"WHERE source_path = ANY(%s) AND corpus = ANY(%s) AND {gen} "
                    "ORDER BY source_path, chunk_index LIMIT %s",
                    (paths, list(corpora), limit),
                )
            rows = cur.fetchall()
        out = []
        for r in rows:
            nats = _row_val(r, "allowed_nationalities", None)
            out.append(SearchResult(
                source_path=r["source_path"], source_title=r["source_title"], chunk_text=r["chunk_text"],
                chunk_index=r["chunk_index"], similarity=float(r["similarity"]),
                combined_score=float(r["similarity"]), corpus=r["corpus"],
                access_tier=_row_val(r, "access_tier", "public"),
                classification=_row_val(r, "classification", "unclassified"),
                allowed_nationalities=tuple(nats) if nats else None,
            ))
        return out

    def _generation_sql(self, corpus_generation: int | None) -> str:
        """The served-generation predicate (:func:`generation_predicate`) for this store."""
        return generation_predicate(
            corpus_generation, has_config=getattr(self, "_has_generation_config", False)
        )

    _CHUNK_COLS = ("source_path, source_title, chunk_text, chunk_index, corpus, "
                   "access_tier, classification, allowed_nationalities")

    def _results(self, rows) -> list[SearchResult]:
        out = []
        for r in rows:
            nats = _row_val(r, "allowed_nationalities", None)
            score = float(_row_val(r, "score", 0.0))
            out.append(SearchResult(
                source_path=r["source_path"], source_title=r["source_title"], chunk_text=r["chunk_text"],
                chunk_index=r["chunk_index"], similarity=0.0, combined_score=score, corpus=r["corpus"],
                access_tier=_row_val(r, "access_tier", "public"),
                classification=_row_val(r, "classification", "unclassified"),
                allowed_nationalities=tuple(nats) if nats else None,
            ))
        return out

    def chunks_at(
        self,
        keys: list[tuple[str, str, int]],
        corpora: list[str] | None = None,
        *,
        corpus_generation: int | None = None,
    ) -> list[SearchResult]:
        """The chunks at ``(source_path, corpus, chunk_index)``, for neighbour expansion (#1984).
        Only within ``corpora``; each carries its access columns for the retriever's filter."""
        if not keys:
            return []
        corpora = list(ALL_CORPORA) if corpora is None else list(corpora)
        wanted = {(p, c, int(i)) for p, c, i in keys}
        with self._cur() as cur:
            cur.execute(
                f"SELECT {self._CHUNK_COLS} FROM chunks "
                "WHERE source_path = ANY(%s) AND chunk_index = ANY(%s) AND corpus = ANY(%s) "
                f"AND {self._generation_sql(corpus_generation)}",
                (sorted({p for p, _c, _i in wanted}), sorted({i for _p, _c, i in wanted}), corpora),
            )
            rows = [r for r in cur.fetchall() if (r["source_path"], r["corpus"], r["chunk_index"]) in wanted]
        return self._results(rows)

    def best_chunks_in(
        self,
        paths: list[str],
        query_text: str,
        corpora: list[str] | None = None,
        *,
        limit: int,
        corpus_generation: int | None = None,
    ) -> list[SearchResult]:
        """Within the sources at ``paths``, the chunks that best match ``query_text`` (#1984).

        A follow-up such as "get the rest of that table" often rewrites to something retrieval
        no longer ties to the document the conversation was about; this finds its best chunks
        inside the documents already cited. Empty when the query has no content terms."""
        tsq = _fts_tsquery(query_text)
        if not paths or not tsq:
            return []
        corpora = list(ALL_CORPORA) if corpora is None else list(corpora)
        with self._cur() as cur:
            cur.execute(
                f"SELECT {self._CHUNK_COLS}, ts_rank(to_tsvector('english', chunk_text), "
                "to_tsquery('english', %s)) AS score FROM chunks "
                f"WHERE source_path = ANY(%s) AND corpus = ANY(%s) AND {self._generation_sql(corpus_generation)} "
                "ORDER BY score DESC, chunk_index LIMIT %s",
                (tsq, sorted(set(paths)), corpora, limit),
            )
            return self._results(cur.fetchall())

    def paths_ending_with(self, suffixes: list[str], corpora: list[str] | None = None, limit: int = 24) -> list[str]:
        """Source paths ending with any of ``suffixes`` (a cited source an answer printed only
        by its last path segments), from ``documents``; LIKE wildcards in a suffix are literal."""
        if not suffixes:
            return []
        corpora = list(ALL_CORPORA) if corpora is None else list(corpora)

        def esc(x: str) -> str:
            return x.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        with self._cur() as cur:
            cur.execute(
                "SELECT DISTINCT source_path FROM documents WHERE corpus = ANY(%s) "
                "AND source_path LIKE ANY(%s) ORDER BY source_path LIMIT %s",
                (corpora, ["%" + esc(x) for x in suffixes], limit),
            )
            return [r["source_path"] for r in cur.fetchall()]

    def stats(self) -> dict:
        """Return index statistics including per-corpus breakdown."""
        with self._cur() as cur:
            cur.execute("SELECT count(*) AS n FROM documents")
            row = cur.fetchone()
            assert row is not None
            total_docs = row["n"]

            cur.execute("SELECT count(*) AS n FROM chunks")
            row = cur.fetchone()
            assert row is not None
            total_chunks = row["n"]

            cur.execute("SELECT corpus, count(*) AS n FROM chunks GROUP BY corpus ORDER BY corpus")
            by_corpus = {r["corpus"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                "SELECT corpus, count(*) AS n FROM documents GROUP BY corpus ORDER BY corpus"
            )
            docs_by_corpus = {r["corpus"]: r["n"] for r in cur.fetchall()}

        return {
            "total_documents": total_docs,
            "total_chunks": total_chunks,
            "chunks_by_corpus": by_corpus,
            "documents_by_corpus": docs_by_corpus,
        }

    # -- community corpus operations ------------------------------------------

    def load_community_dump(self, dump_path: Path) -> None:
        """Load a pre-built community corpus from a pg_dump file.

        Clears the existing rag-community corpus first, then restores.
        The dump must contain only the chunks/documents tables filtered to
        corpus='rag-community'.

        Args:
            dump_path: Path to the .sql or .pgdump file to restore.
        """
        log.info("Loading community corpus from %s", dump_path)

        # Clear existing community data
        deleted = self.delete_corpus(CORPUS_COMMUNITY)
        if deleted > 0:
            log.info("Cleared %d existing community chunks before reload", deleted)

        # Use psql to execute the dump directly
        cmd = ["psql", self._dsn, "-f", str(dump_path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            log.info("Community corpus loaded: %s", result.stdout.strip() or "ok")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to load community corpus from {dump_path}:\n{exc.stderr}"
            ) from exc
        except FileNotFoundError:
            raise RuntimeError("psql not found — ensure PostgreSQL client tools are installed")

    def load_pack_csv(self, pack_dir: Path) -> dict:
        """Load a community knowledge pack from CSV files.

        Expects ``chunks.csv`` and ``documents.csv`` in *pack_dir*.
        Clears the existing community corpus first, then bulk-loads.
        Returns dict with loaded counts.
        """
        import csv

        chunks_csv = pack_dir / "chunks.csv"
        docs_csv = pack_dir / "documents.csv"
        if not chunks_csv.exists() or not docs_csv.exists():
            raise FileNotFoundError(f"Pack at {pack_dir} missing chunks.csv or documents.csv")

        log.info("Loading community pack from %s", pack_dir)
        deleted = self.delete_corpus(CORPUS_COMMUNITY)
        if deleted > 0:
            log.info("Cleared %d existing community chunks", deleted)

        assert self._conn is not None
        cur = self._conn.cursor()

        # Load documents
        with open(docs_csv, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            doc_count = 0
            for row in reader:
                cur.execute(
                    """INSERT INTO documents
                        (source_path, corpus, source_type, title, checksum,
                         content_hash, chunk_count, owner, data_source, sync_id,
                         first_indexed, last_indexed)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_path, corpus) DO UPDATE SET
                        chunk_count=EXCLUDED.chunk_count, last_indexed=EXCLUDED.last_indexed""",
                    (
                        row["source_path"],
                        CORPUS_COMMUNITY,
                        row.get("source_type", "pdf"),
                        row.get("title", ""),
                        row.get("checksum", ""),
                        row.get("content_hash", ""),
                        int(row.get("chunk_count", 0)),
                        row.get("owner") or None,
                        row.get("data_source", "pack"),
                        row.get("sync_id", ""),
                        row.get("first_indexed", datetime.now(UTC)),
                        row.get("last_indexed", datetime.now(UTC)),
                    ),
                )
                doc_count += 1

        # Load chunks in batches
        now_str = datetime.now(UTC).isoformat()
        with open(chunks_csv, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            chunk_count = 0
            batch = []
            for row in reader:
                emb = row.get("embedding", "")
                emb_val = emb if emb and emb not in ("None", "null", "") else None
                batch.append(
                    (
                        row["source_path"],
                        clean_title(row.get("source_title", ""), row["source_path"]),
                        row.get("source_type", "pdf"),
                        row["chunk_text"],
                        int(row.get("chunk_index", 0)),
                        int(row.get("start_line", 1)),
                        emb_val,
                        CORPUS_COMMUNITY,
                        row.get("owner") or None,
                        row.get("team") or None,
                        row.get("checksum", ""),
                        row.get("chunking_tier", "fixed"),
                        row.get("indexed_at", now_str),
                        row.get("updated_at", now_str),
                    )
                )
                chunk_count += 1
                if len(batch) >= 500:
                    psycopg2.extras.execute_batch(
                        cur,
                        """
                        INSERT INTO chunks (source_path, source_title, source_type,
                            chunk_text, chunk_index, start_line, embedding, corpus,
                            owner, team, checksum, chunking_tier, indexed_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        batch,
                    )
                    batch = []
            if batch:
                psycopg2.extras.execute_batch(
                    cur,
                    """
                    INSERT INTO chunks (source_path, source_title, source_type,
                        chunk_text, chunk_index, start_line, embedding, corpus,
                        owner, team, checksum, chunking_tier, indexed_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    batch,
                )

        self._conn.commit()

        # Warn if chunks lack embeddings
        with self._cur() as cur2:
            cur2.execute(
                "SELECT count(*) AS n FROM chunks WHERE corpus = %s AND embedding IS NULL",
                (CORPUS_COMMUNITY,),
            )
            missing = cur2.fetchone()["n"]
            if missing > 0:
                log.warning(
                    "%d/%d community chunks have no embeddings — run embedding pipeline to fix",
                    missing,
                    chunk_count,
                )

        log.info("Loaded %d documents, %d chunks from pack", doc_count, chunk_count)
        return {"documents": doc_count, "chunks": chunk_count}
