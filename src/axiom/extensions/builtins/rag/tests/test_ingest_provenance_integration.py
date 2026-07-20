# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DB-gated integration tests for the v0.22.0 RAG ingest / provenance / audit pipeline.

Require a live PostgreSQL instance with pgvector. They skip cleanly when no test
database is configured, so the default (non-integration) suite stays green.
Run with:
    pytest src/axiom/extensions/builtins/rag/tests/test_ingest_provenance_integration.py \
        -m integration -v

DATABASE_URL is read from the ``DATABASE_URL`` environment variable or the
``rag.database_url`` setting — the same resolution the shipped store CLI uses.
Tests isolate their data under the ``rag-internal`` / ``rag-org`` corpora with a
test-unique ``integration-test/`` path prefix and clean up before and after.

ISOLATION RULE — read before adding a test here:
    The ``store`` fixture is module-scoped against a single Postgres instance.
    Under pytest-xdist, tests run in parallel workers sharing that DB. Any test
    that asserts on row count / presence for a given source_path MUST use a
    source_path unique to that test. The fixtures below build their corpora
    inside per-test ``tmp_path`` directories whose *relative* paths all start
    with the shared ``TEST_PREFIX`` (because the on-disk subtree names are
    test-specific), so collisions across workers do not occur.

WHY A FAKE EMBEDDER:
    ``embed_texts`` is monkeypatched per-test to a deterministic fake so that
    vector retrieval is reproducible without a live embedding provider. The fake
    keys a 768-dim one-hot vector off a sentinel token planted in each fixture
    doc; a query carrying the same sentinel produces an identical vector, so the
    pgvector cosine search returns that exact chunk. Storage uses the *real*
    test Postgres — only the embedder is faked.
"""

from __future__ import annotations

import os
import textwrap

import pytest

# ---------------------------------------------------------------------------
# DB resolution + skip guard (mirrors test_rag_integration.py exactly)
# ---------------------------------------------------------------------------


def _get_db_url() -> str | None:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return SettingsStore().get("rag.database_url") or None
    except Exception:
        return None


def _requires_db(fn):
    """Decorator: skip if no DATABASE_URL configured, else mark integration."""
    url = _get_db_url()
    return pytest.mark.skipif(
        not url,
        reason="DATABASE_URL / rag.database_url not configured",
    )(pytest.mark.integration(fn))


# ---------------------------------------------------------------------------
# Constants + deterministic fake embedder
# ---------------------------------------------------------------------------

TEST_PREFIX = "integration-test/"
TEST_CORPUS = "rag-internal"
TEST_ORG_CORPUS = "rag-org"
DIMS = 768

# Sentinel tokens planted in fixture docs. The fake embedder maps each sentinel
# to a distinct one-hot dimension so a query carrying the sentinel lands an exact
# cosine match on the chunk that contains it. Tokens are domain-neutral.
_SENTINELS: dict[str, int] = {
    "alphafact": 0,
    "betafact": 1,
    "gammafact": 2,
    "deltafact": 3,
    "epsilonfact": 4,
    "zetafact": 5,
}


def _fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic 768-dim embedder.

    Each text's vector is the (normalized) sum of one-hot vectors for whichever
    sentinel tokens it contains; if none are present, dimension 700 is set so the
    vector is still non-null and storable. Same text → same vector, so a query
    carrying a sentinel matches the chunk containing that sentinel exactly.
    """
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * DIMS
        lowered = text.lower()
        hit = False
        for token, dim in _SENTINELS.items():
            if token in lowered:
                vec[dim] = 1.0
                hit = True
        if not hit:
            vec[700] = 1.0
        out.append(vec)
    return out


def _query_vec(sentinel: str) -> list[float]:
    """The query embedding for a known sentinel — one-hot at its dimension."""
    vec = [0.0] * DIMS
    vec[_SENTINELS[sentinel]] = 1.0
    return vec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _schema_ready(tmp_path_factory):
    """Create the pgvector extension + RAG schema exactly once, xdist-safe.

    Postgres' ``CREATE EXTENSION/TABLE IF NOT EXISTS`` checks race on the system
    catalogs when several xdist workers touch a *cold* DB at the same instant
    (losers hit ``UniqueViolation`` on pg_type/pg_extension, or ``DeadlockDetected``
    on the concurrent ALTER). The standard xdist idiom avoids the race entirely:
    one worker bootstraps the schema under a cross-worker file lock; the others
    wait, then connect to an already-warm schema. (In CI the schema is pre-created
    by the DB setup step, so this is effectively a no-op there.)

    ``PYTEST_XDIST_WORKER`` is read directly rather than via the ``worker_id``
    fixture so the test still runs under ``-p no:xdist`` (where that fixture is
    absent): unset means we are the sole process and bootstrap directly.
    """
    url = _get_db_url()
    if not url:
        pytest.skip("No database configured")

    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        # Not running under xdist — no other worker to race with.
        _bootstrap_schema(url)
        return

    from filelock import FileLock

    root_tmp = tmp_path_factory.getbasetemp().parent
    lock = root_tmp / "rag_itest_schema.lock"
    flag = root_tmp / "rag_itest_schema.done"
    with FileLock(str(lock)):
        if not flag.exists():
            _bootstrap_schema(url)
            flag.write_text("ok")


def _bootstrap_schema(url: str) -> None:
    from axiom.extensions.builtins.signals.migrations import ensure_pgvector_extension
    from axiom.rag.store import RAGStore

    ensure_pgvector_extension()
    s = RAGStore(url)
    _connect_with_retry(s)  # runs the idempotent schema DDL once
    # Sweep any leftovers from a previous failed run across all test namespaces.
    _clean_prefix(s, TEST_PREFIX)
    s.close()


def _connect_with_retry(s, attempts: int = 8) -> None:
    """Connect, tolerating the cold-start schema-DDL race under pytest-xdist.

    ``RAGStore.connect()`` re-runs idempotent schema DDL (CREATE EXTENSION / TABLE /
    ALTER TABLE ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS) on *every*
    call. When several xdist workers connect at the same instant against a cold
    DB, Postgres' ``IF NOT EXISTS`` checks race on the system catalogs — losers see
    ``DeadlockDetected`` (concurrent ALTER taking AccessExclusiveLock) or
    ``UniqueViolation`` (duplicate pg_type/pg_extension row). The DDL is idempotent,
    so a short retry succeeds once any worker wins. (In CI the schema is pre-created
    by the DB setup step, so this path is a no-op there.)
    """
    import time

    from psycopg2.errors import DeadlockDetected, UniqueViolation

    for attempt in range(attempts):
        try:
            s.connect()
            return
        except (DeadlockDetected, UniqueViolation):
            s.close()
            if attempt == attempts - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


@pytest.fixture(scope="module")
def store(_schema_ready):
    """Connected RAGStore. Schema is bootstrapped once (xdist-safe) by _schema_ready;
    per-test data isolation is handled by the ``corpus`` fixture's unique namespace,
    so this fixture does NOT run a prefix-wide delete (that would wipe data of tests
    running concurrently on other xdist workers)."""
    url = _get_db_url()
    if not url:
        pytest.skip("No database configured")
    from axiom.rag.store import RAGStore

    s = RAGStore(url)
    _connect_with_retry(s)
    yield s
    s.close()


def _retry_db(fn, attempts: int = 6):
    """Run a DB-mutating callable, retrying on a deadlock.

    ``RAGStore`` writes (DELETE+INSERT in ``upsert_chunks``, the audit purge, the
    namespace cleanup) can deadlock when several xdist workers mutate the shared
    ``chunks``/``documents`` tables at the same instant. A deadlock rolls back the
    victim transaction cleanly, so re-running the operation succeeds. This keeps
    the test suite stable under ``-n auto`` without altering shipped store code.
    """
    import time

    from psycopg2.errors import DeadlockDetected

    for attempt in range(attempts):
        try:
            return fn()
        except DeadlockDetected:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _clean_prefix(s, prefix: str) -> None:
    """Delete every row whose source_path starts with *prefix*, across all corpora."""

    def _do():
        with s._cur() as cur:
            cur.execute("DELETE FROM chunks WHERE source_path LIKE %s", (prefix + "%",))
            cur.execute("DELETE FROM documents WHERE source_path LIKE %s", (prefix + "%",))

    _retry_db(_do)


def _write_corpus(root, files: dict[str, str]) -> None:
    """Materialize a {relative_path: content} mapping under *root*."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


@pytest.fixture
def corpus(store, tmp_path):
    """Factory for a per-test corpus with an isolated DB namespace.

    Returns ``build(subdir, files) -> (ingest_root, rel_root)``:
      - ``ingest_root`` is ``tmp_path`` (pass to ingest_path);
      - ``rel_root`` is the subtree path relative to ingest_root, beginning with
        ``TEST_PREFIX`` so every stored source_path carries the isolation prefix.

    Each built subtree's stored rows are deleted on teardown — scoped to *this*
    test's unique ``TEST_PREFIX/<subdir>`` namespace, never a prefix-wide sweep,
    so parallel xdist workers cannot wipe each other's in-flight data. ingest
    computes source_path relative to the root it is given, so the root must be
    ``tmp_path`` (not the subtree).
    """
    built: list[str] = []

    def build(subdir: str, files: dict[str, str]):
        rel_root = f"{TEST_PREFIX}{subdir}"
        _write_corpus(tmp_path / TEST_PREFIX / subdir, files)
        built.append(rel_root)
        return tmp_path, rel_root

    yield build

    for rel_root in built:
        _clean_prefix(store, rel_root)


# ---------------------------------------------------------------------------
# 1. End-to-end ingest → store → retrieve
# ---------------------------------------------------------------------------


@_requires_db
def test_ingest_path_roundtrip_is_retrievable(store, corpus, monkeypatch):
    """ingest_path indexes a small corpus; chunk text comes back via store.search."""
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest import ingest_path

    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)

    ingest_root, rel_root = corpus(
        "rt-corpus",
        {
            "intro.md": textwrap.dedent("""\
                # Intro

                The system uses a layered cache. Sentinel: alphafact.
                A negative feedback loop keeps it stable under load.
            """),
            "ops/handbook.txt": textwrap.dedent("""\
                Operations handbook. Sentinel: betafact.
                Restart procedures flush pending writes before shutdown.
            """),
        },
    )

    stats = _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_CORPUS))

    assert stats.files_indexed == 2
    assert stats.chunks_created >= 2
    assert stats.files_excluded == 0
    assert stats.files_quarantined == 0

    # Both documents are present in the store.
    paths = store.list_document_paths(TEST_CORPUS)
    assert f"{rel_root}/intro.md" in paths
    assert f"{rel_root}/ops/handbook.txt" in paths

    # Vector retrieval is deterministic: query for the sentinel returns its chunk.
    results = store.search(
        query_embedding=_query_vec("alphafact"),
        query_text="alphafact cache feedback",
        corpora=[TEST_CORPUS],
        limit=5,
    )
    assert results, "expected at least one hit for the alphafact sentinel"
    assert any("alphafact" in r.chunk_text.lower() for r in results)
    top = results[0]
    assert top.source_path == f"{rel_root}/intro.md"
    assert top.similarity > 0.99  # exact one-hot cosine match


# ---------------------------------------------------------------------------
# 2. Provenance gate end-to-end
# ---------------------------------------------------------------------------


@_requires_db
def test_provenance_gate_excludes_and_quarantines(store, corpus, monkeypatch):
    """Exclude folder + quarantine artifact never hit the store; allowed docs do."""
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest import ingest_path
    from axiom.rag.ingest_router import Disposition, ProvenanceRule

    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)

    ingest_root, rel_root = corpus(
        "prov-corpus",
        {
            # ordinary, allowed
            "public/guide.md": "# Guide\n\nPublic guidance. Sentinel: gammafact.\n",
            "public/notes.md": "# Notes\n\nMore public notes. Sentinel: deltafact.\n",
            # to-exclude folder (licensed/controlled source)
            "vendor/manual.md": "# Vendor manual\n\nLicensed content. Sentinel: gammafact.\n",
            "vendor/spec.md": "# Vendor spec\n\nLicensed spec. Sentinel: deltafact.\n",
            # to-quarantine artifact (opaque archive — hold for human review)
            "drop/bundle.zip": "PK\x03\x04 not really a zip",
        },
    )

    rules = [
        ProvenanceRule(f"{rel_root}/vendor/", Disposition.EXCLUDE, reason="licensed vendor"),
        ProvenanceRule(f"{rel_root}/drop/*.zip", Disposition.QUARANTINE, reason="archive"),
    ]

    stats = _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_CORPUS, rules=rules))

    # Only the two public docs were indexed.
    assert stats.files_indexed == 2
    assert stats.files_excluded == 2
    assert stats.files_quarantined == 1
    assert stats.excluded_by_rule == {f"{rel_root}/vendor/": 2}

    # The store holds only the allowed docs — excluded/quarantined never landed.
    paths = set(store.list_document_paths(TEST_CORPUS))
    assert f"{rel_root}/public/guide.md" in paths
    assert f"{rel_root}/public/notes.md" in paths
    assert f"{rel_root}/vendor/manual.md" not in paths
    assert f"{rel_root}/vendor/spec.md" not in paths
    assert f"{rel_root}/drop/bundle.zip" not in paths

    # The drop report is honest about what was gated out.
    report = stats.drop_report()
    assert "excluded by provenance" in report
    assert "quarantined" in report


# ---------------------------------------------------------------------------
# 3. audit_paths over a populated store + --purge via the CLI seam
# ---------------------------------------------------------------------------


@_requires_db
def test_audit_flags_and_purge_removes_only_excluded(store, corpus, monkeypatch):
    """audit_paths flags a controlled doc already in the store; --purge deletes EXCLUDE only."""
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest import ingest_path
    from axiom.rag.ingest_router import Disposition, ProvenanceRule, audit_paths

    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)

    # Ingest *ungated* (e.g. before any rules existed) so controlled content
    # is already present, into the org tier the audit verb defaults to.
    ingest_root, rel_root = corpus(
        "audit-corpus",
        {
            "open/readme.md": "# Readme\n\nOpen content. Sentinel: epsilonfact.\n",
            "restricted/secret.md": "# Restricted\n\nControlled content. Sentinel: zetafact.\n",
            "holding/incoming.md": "# Incoming\n\nUnvetted content. Sentinel: alphafact.\n",
        },
    )
    _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_ORG_CORPUS))

    # A rule set that now flags the restricted folder (exclude) and the holding
    # folder (quarantine).
    rules = [
        ProvenanceRule(f"{rel_root}/restricted/", Disposition.EXCLUDE, reason="controlled"),
        ProvenanceRule(f"{rel_root}/holding/", Disposition.QUARANTINE, reason="unvetted"),
    ]

    # audit_paths over the live store paths (the `axi rag audit` data path).
    paths = store.list_document_paths(TEST_ORG_CORPUS)
    report = audit_paths(paths, rules)
    flagged = {p: d for p, d, _ in report.flagged}
    assert flagged.get(f"{rel_root}/restricted/secret.md") is Disposition.EXCLUDE
    assert flagged.get(f"{rel_root}/holding/incoming.md") is Disposition.QUARANTINE
    assert f"{rel_root}/open/readme.md" not in flagged
    assert report.excluded == 1
    assert report.quarantined == 1

    # --purge deletes ONLY the EXCLUDE-flagged doc; quarantine + open survive.
    to_purge = [p for p, d, _ in report.flagged if d is Disposition.EXCLUDE]
    for p in to_purge:
        _retry_db(lambda p=p: store.delete_document(p, TEST_ORG_CORPUS))

    remaining = set(store.list_document_paths(TEST_ORG_CORPUS))
    assert f"{rel_root}/restricted/secret.md" not in remaining  # purged
    assert f"{rel_root}/holding/incoming.md" in remaining  # quarantine-flagged, NOT purged
    assert f"{rel_root}/open/readme.md" in remaining  # never flagged


@_requires_db
def test_cli_audit_purge_round_trip(store, corpus, monkeypatch):
    """`axi rag audit --purge --yes` flags + removes EXCLUDE docs through the real CLI."""
    from axiom.rag import cli as rag_cli
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest import ingest_path

    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)
    monkeypatch.setattr(rag_cli, "_get_store", lambda: store)

    ingest_root, rel_root = corpus(
        "cli-audit-corpus",
        {
            "ok/intro.md": "# Intro\n\nKeep me. Sentinel: betafact.\n",
            "vendor/licensed.md": "# Licensed\n\nPurge me. Sentinel: gammafact.\n",
        },
    )
    _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_ORG_CORPUS))

    rules_file = ingest_root / "rules.toml"
    rules_file.write_text(
        textwrap.dedent(f"""\
            [[rule]]
            pattern = "{rel_root}/vendor/"
            disposition = "exclude"
            reason = "licensed vendor"
        """),
        encoding="utf-8",
    )

    # Dry audit (no purge) — reports the flagged doc but leaves it in place.
    rag_cli.main(["audit", "--corpus", TEST_ORG_CORPUS, "--rules", str(rules_file)])
    paths_before = set(store.list_document_paths(TEST_ORG_CORPUS))
    assert f"{rel_root}/vendor/licensed.md" in paths_before

    # Purge with confirmation skip.
    _retry_db(
        lambda: rag_cli.main(
            [
                "audit",
                "--corpus",
                TEST_ORG_CORPUS,
                "--rules",
                str(rules_file),
                "--purge",
                "--yes",
            ]
        )
    )
    paths_after = set(store.list_document_paths(TEST_ORG_CORPUS))
    assert f"{rel_root}/vendor/licensed.md" not in paths_after  # purged
    assert f"{rel_root}/ok/intro.md" in paths_after  # untouched


# ---------------------------------------------------------------------------
# 4. Resume / durability
# ---------------------------------------------------------------------------


@_requires_db
def test_reingest_skips_unchanged_checksum(store, corpus, monkeypatch):
    """Re-running ingest over an unchanged corpus does no double-work (checksum skip)."""
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest import ingest_path

    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)

    ingest_root, _rel_root = corpus(
        "resume-corpus",
        {
            "a.md": "# A\n\nFirst doc. Sentinel: deltafact.\n",
            "b.md": "# B\n\nSecond doc. Sentinel: epsilonfact.\n",
        },
    )

    first = _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_CORPUS))
    assert first.files_indexed == 2
    assert first.files_unchanged == 0

    second = _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_CORPUS))
    assert second.files_indexed == 0  # nothing re-indexed
    assert second.files_unchanged == 2  # both skipped on matching checksum


@_requires_db
def test_embed_failure_leaves_doc_unindexed_then_later_run_indexes_it(
    store, corpus, monkeypatch
):
    """A transient EmbeddingError leaves the doc uncommitted; a later run indexes it."""
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.embeddings import EmbeddingError
    from axiom.rag.ingest import ingest_path

    ingest_root, rel_root = corpus(
        "durability-corpus",
        {"flaky.md": "# Flaky\n\nDoc that fails to embed first. Sentinel: zetafact.\n"},
    )
    doc_path = f"{rel_root}/flaky.md"

    # Run 1: the embedder is configured but down → EmbeddingError. The doc must
    # NOT be committed (else its checksum is recorded and it's skipped forever).
    def _boom(_texts):
        raise EmbeddingError("network drop to embedder")

    monkeypatch.setattr(ingest_mod, "embed_texts", _boom)
    failed = _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_CORPUS))
    assert failed.files_failed == 1
    assert failed.files_indexed == 0
    assert store.get_document(doc_path, corpus=TEST_CORPUS) is None  # uncommitted

    # Run 2: the embedder recovers → the same doc is now indexed and retrievable.
    monkeypatch.setattr(ingest_mod, "embed_texts", _fake_embed)
    recovered = _retry_db(lambda: ingest_path(ingest_root, store, corpus=TEST_CORPUS))
    assert recovered.files_indexed == 1
    assert recovered.files_unchanged == 0  # was never committed, so not "unchanged"

    doc = store.get_document(doc_path, corpus=TEST_CORPUS)
    assert doc is not None
    assert doc["chunk_count"] >= 1

    results = store.search(
        query_embedding=_query_vec("zetafact"),
        corpora=[TEST_CORPUS],
        limit=5,
    )
    assert any("zetafact" in r.chunk_text.lower() for r in results)
